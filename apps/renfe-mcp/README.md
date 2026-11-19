# MCP de Renfe

Servidor MCP con estaciones, horarios y precios de Renfe y retrasos en directo. Es una capa fina sobre
[renfe-cli](https://github.com/luisgonzaleznf/renfe-cli) de Luis González (MIT): cada herramienta devuelve el
mismo JSON que el comando `renfe` equivalente. No es oficial: usa la API pública de la web de Renfe.

| Herramienta     | Qué hace                                                     |
| --------------- | ------------------------------------------------------------ |
| `find_stations` | Estaciones y grupos de ciudad por nombre o código            |
| `search_trains` | Trenes, horarios y precios para un adulto en una fecha       |
| `train_status`  | Retraso en directo y próxima estación de un tren que circula |
| `list_delays`   | Todos los trenes con retraso en el mapa en directo           |

Se publica en `https://reforma-digital.es/mcp/renfe`.

## Despliegue

Es Python, así que va en su propio proyecto de Vercel y la web lo publica en su dominio:

1. Proyecto de Vercel nuevo sobre este repositorio con **Root Directory** `apps/renfe-mcp`. Vercel detecta
   `main.py` y sirve su `app` (Streamable HTTP en `/mcp`). `vercel.json` lo fija en París (`cdg1`), cerca de Renfe.
2. En el proyecto de la web, la variable `RENFE_MCP_URL` con la URL de ese proyecto (por ejemplo
   `https://renfe-mcp.vercel.app`). Sin ella, `/mcp/renfe` no existe.
3. En el cortafuegos de la web, una regla que deje pasar `/mcp/renfe` sin el desafío anti-bots, con límite de
   peticiones: los clientes MCP son automáticos.

## En local

```sh
uv run main.py                          # stdio
uv run uvicorn main:app --port 8000     # http://127.0.0.1:8000/mcp
```

renfe-cli está fijado a un commit en `pyproject.toml` porque el servidor usa sus helpers internos; al
actualizarlo, comprueba que siguen existiendo.
