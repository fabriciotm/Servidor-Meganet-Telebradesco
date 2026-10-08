# API pública — Servidor MegaNet / Telebradesco

A integração Python recomendada é feita por `meganet_server.api`. O módulo `meganet_server.server` permanece como fachada de compatibilidade para integrações existentes.

## Versão

```python
from meganet_server import __version__
```

## CLI e configuração

```python
from meganet_server.api import parse_args, main
from meganet_server.config import AppConfig, default_operational_config
```

`parse_args()` aplica a precedência CLI explícita > JSON > defaults e anexa a visão tipada `AppConfig` ao namespace operacional.

## Runtime

As integrações de runtime incluem startup/shutdown dos listeners, transporte TCP/serial, listener unificado, estatísticas e dispatch para as sessões MegaNet e Telebradesco.

```python
from meganet_server.api import run_services
```

## MegaNet

Os módulos públicos de domínio ficam em `meganet_server.meganet`:

- `protocol` — framing, checksum e parser incremental;
- `arq` — ACK/NAK e retransmissão;
- `accounts` — cadastro, autenticação e código de acesso;
- `content` / `content_store` — conteúdo editorial e persistência;
- `mail` — correio interno;
- `session` — máquina de estados;
- `transport` — transporte da sessão.

## Telebradesco

Os módulos públicos de domínio ficam em `meganet_server.telebradesco`:

- `protocol` — framing Class-1, bit reversal, DLE stuffing e login;
- `accounts` — identidade, autenticação e split store;
- `content` — conteúdo financeiro e compilação do stream;
- `adapter` — protocolo interno `TBA1/TBR1`;
- `session` — máquina de estados.

## Administração web

```python
from meganet_server.api import start_admin_server
```

A interface administrativa é implementada em `meganet_server.admin` e não deve ser importada pelas camadas de protocolo/runtime.

## Compatibilidade

`meganet_server.server` resolve aliases históricos de forma lazy para não quebrar scripts existentes. Código novo deve preferir `meganet_server.api` ou os módulos de domínio diretamente.
