"""Servidor MCP de Renfe: estaciones, trenes y precios, y retrasos en directo.

Una capa fina sobre renfe-cli (https://github.com/luisgonzaleznf/renfe-cli, MIT): cada herramienta hace lo
mismo que el comando `renfe` equivalente y devuelve el mismo JSON. Reutiliza los helpers de la CLI, por eso
renfe-cli está fijado a un commit en pyproject.toml.

En Vercel se sirve `app` (Streamable HTTP en /mcp). En local: `uv run main.py` (stdio) o `uv run main.py http`.
"""

import json
import os
import sys
from datetime import datetime
from typing import Annotated, Any

from pydantic import Field
from renfe import cli, delays as delays_mod, live, search as search_mod, stations as stations_mod
from renfe.errors import RenfeError

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from starlette.responses import PlainTextResponse

if os.environ.get("VERCEL"):  # solo /tmp se puede escribir
    os.environ.setdefault("RENFE_CACHE_DIR", "/tmp/renfe-cli")

INSTRUCTIONS = """\
Trenes de Renfe: busca estaciones, lista trenes con horarios y precios en una fecha y consulta retrasos en directo.
No oficial: usa la API pública de la web de Renfe, que puede cambiar o rechazar peticiones sin aviso.

- Las estaciones aceptan un nombre («Madrid», «Atocha», «Sevilla») o un código de Renfe («MADRI», «60000»); los
  grupos de ciudad como «Madrid (Todas)» incluyen todas sus estaciones. Usa find_stations si hay dudas.
- Las fechas son AAAA-MM-DD, «today» o «tomorrow», en hora de Madrid, hasta un año vista.
- Los precios son para un adulto, del más barato al más caro. Cada resultado trae un enlace `shop` para comprar
  en Renfe; este servidor no compra billetes.
- Los retrasos en directo cubren larga y media distancia, solo mientras el tren circula.
- Un error de tipo «blocked» significa que Renfe rechazó o limitó la petición: no reintentes enseguida.
"""

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)

mcp = MCPServer(name="renfe", title="Trenes de Renfe", instructions=INSTRUCTIONS)


def _run(produce: Any) -> dict:
    """El `_emit` de la CLI para MCP: un RenfeError se devuelve con su JSON; cualquier otro error, solo con su
    tipo, porque el mensaje puede llevar URLs, cabeceras o tokens."""
    try:
        return produce()
    except RenfeError as err:
        raise ToolError(json.dumps(err.to_dict(), ensure_ascii=False)) from None
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(f"unexpected internal error: {type(exc).__name__}") from None


@mcp.tool(title="Buscar estaciones", annotations=READ_ONLY)
def find_stations(
    query: Annotated[str, Field(description="Parte del nombre de una estación o ciudad, o un código de Renfe.")],
    limit: Annotated[int, Field(ge=1, le=100, description="Máximo de estaciones.")] = 10,
) -> dict:
    """Estaciones y grupos de ciudad que coinciden con un nombre o código, la mejor primero, con sugerencias si
    hay una errata."""

    def produce() -> dict:
        def pick(every: list) -> list:
            return stations_mod.find(query, every, limit)

        found = pick(stations_mod.bundled())
        if not found:  # nada en la lista incluida: se pide la lista actual a Renfe
            with cli._client() as client:
                found = pick(stations_mod.load(client, refresh=False))
        out = {"query": query, "count": len(found), "stations": [s.to_dict() for s in found]}
        if not found:
            out["suggestions"] = [s.to_dict() for s in stations_mod.suggestions(query)]
        return out

    return _run(produce)


@mcp.tool(title="Buscar trenes", annotations=READ_ONLY)
def search_trains(
    origin: Annotated[str, Field(description="Origen: nombre (Madrid, Atocha) o código (MADRI, 60000).")],
    destination: Annotated[str, Field(description="Destino: nombre o código de estación.")],
    date: Annotated[str, Field(description="Fecha del viaje: AAAA-MM-DD, today o tomorrow (hora de Madrid).")] = "today",
    after: Annotated[str | None, Field(
        description="Solo trenes que salen a partir de HH:MM, hora local. Hoy, por defecto, desde ahora.")] = None,
    arrive_by: Annotated[str | None, Field(
        description="Solo trenes que llegan antes de HH:MM en la fecha del viaje, hora de Madrid.")] = None,
    include_fares: Annotated[bool, Field(
        description="Incluir precios para un adulto (una petición más a Renfe por cada 3 trenes).")] = True,
    limit: Annotated[int, Field(ge=1, le=50, description="Máximo de trenes, por hora de salida.")] = 10,
) -> dict:
    """Trenes entre dos estaciones en una fecha: salida y llegada, duración, transbordos, tipo de tren y precios
    para un adulto. Para hoy, también el retraso en directo de cada tren. Incluye un enlace para comprar."""

    def produce() -> dict:
        day, after_time = cli._parse_date(date), cli._parse_time(after, "after")
        arrival_time = cli._parse_time(arrive_by, "arrive_by")
        now = datetime.now(search_mod.MADRID)
        today = day == now.date()
        with cli._client() as client:
            src, dst = cli._stations(client, origin, destination)
            delays = live.in_background(client.transport) if today else None
            out = search_mod.search(client, src, dst, day, names=cli._names(src, dst), after=after_time,
                                    fares=include_fares, arrive_by=arrival_time, limit=limit, now=now,
                                    from_now=today and after_time is None)
        board, why, map_retries = delays() if delays else (None, None, [])
        try:
            live.annotate(out["trains"], board)
        except Exception as exc:  # el mapa en directo nunca hace fallar una búsqueda
            live.annotate(out["trains"], None)
            board, why = None, f"{live.LABEL}: unexpected error ({type(exc).__name__})"
        out["delays_included"] = board is not None
        if why:
            cli._add_notice(out, f"Delays are unknown ({why}).")
        out["retries"] = client.retries + map_retries
        return out

    return _run(produce)


@mcp.tool(title="Estado de un tren", annotations=READ_ONLY)
def train_status(
    train: Annotated[str, Field(description="Número de tren como en el billete o el mapa, por ejemplo 03081.")],
) -> dict:
    """Retraso en directo de un tren que circula, su próxima estación y la llegada prevista a ella (larga y media
    distancia)."""

    def produce() -> dict:
        number = cli._train_number(train)
        with cli._client() as client:
            board = live.fetch(transport=client.transport, retries=client.retries)
            return delays_mod.status(board, number, cli._names(), transport=client.transport,
                                     retries=client.retries)

    return _run(produce)


@mcp.tool(title="Trenes con retraso", annotations=READ_ONLY)
def list_delays(
    train: Annotated[str | None, Field(
        description="Solo este tren: su número en directo o uno de billete verificado. Sin él, todos.")] = None,
) -> dict:
    """Todos los trenes con retraso ahora en el mapa en directo de Renfe, con sus horas previstas de salida y
    llegada y los números de tren de cada tramo."""

    def produce() -> dict:
        wanted = cli._train_number(train) if train is not None else None
        with cli._client() as client:
            board = live.fetch(transport=client.transport, retries=client.retries)
            return delays_mod.report(board, cli._names(), transport=client.transport, retries=client.retries,
                                     train=wanted)

    return _run(produce)


def http_app():
    """Streamable HTTP sin estado y con respuestas JSON: cualquier instancia responde a cualquier petición.
    La comprobación de Host protege servidores locales del DNS rebinding; en Vercel el servicio es público y
    le llegan peticiones reenviadas desde la web, así que allí se desactiva."""
    local = not os.environ.get("VERCEL")
    return mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=local, allowed_hosts=["127.0.0.1:*", "localhost:*"]),
    )


async def app(scope, receive, send):
    """La app del SDK necesita su lifespan abierto, y un entorno serverless puede no ejecutarlo. Sin estado,
    cada petición monta una app nueva y abre su lifespan ella misma."""
    inner = http_app()
    if scope["type"] != "http":  # el lifespan de un servidor que sí lo ejecuta
        return await inner(scope, receive, send)
    if scope["method"] != "POST":
        # Sin estado, un GET abriría un stream SSE que nunca acaba y tendría la función viva hasta su límite.
        return await PlainTextResponse("Method Not Allowed", 405, {"Allow": "POST"})(scope, receive, send)
    async with inner.router.lifespan_context(inner):
        await inner(scope, receive, send)


if __name__ == "__main__":
    if sys.argv[1:2] == ["http"]:
        import uvicorn

        uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))
    else:
        mcp.run()
