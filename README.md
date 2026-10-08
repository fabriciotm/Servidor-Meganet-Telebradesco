# Servidor MegaNet / Telebradesco

Servidor em Python que emula os serviços de rede utilizados pelos cartuchos **MegaNet** e **Telebradesco** do Mega Drive. O projeto oferece transporte TCP e serial, suporte a integração com módulos Asterisk, conteúdo editável, contas de demonstração, correio MegaNet e uma interface administrativa web.

Pode ser usado o emulador Bizhawk modificado para conectar via TCP ou serial:

https://github.com/fabriciotm/BizHawk-Meganet_Telebradesco

> Os dados incluídos no repositório são inteiramente fictícios e existem apenas para demonstração e testes.

## O que já vem pronto

Ao baixar o projeto, você já encontra uma configuração local funcional e dados de exemplo para os dois serviços:

- MegaNet em TCP `127.0.0.1:19777`;
- Telebradesco em TCP `127.0.0.1:19779`;
- adapter Telebradesco em `127.0.0.1:19780`;
- administração web em `http://127.0.0.1:19778`;
- conteúdo MegaNet preenchido nos grupos Tectoy News, Dicas, Sega Club, Internet, Revista e Outros;
- usuário e caixa postal MegaNet de demonstração;
- conta Telebradesco com extrato, lançamentos futuros, poupança, investimentos e cartão fictícios.

## Requisitos

- Python **3.11 ou superior** recomendado;
- `pip`;
- `pyserial` para transporte serial/COM.

A interface web e os servidores TCP usam apenas a biblioteca padrão do Python. Para executar os testes de desenvolvimento, instale também `requirements-dev.txt`.

## Instalação no Windows

No PowerShell ou Prompt de Comando:

```powershell
git clone https://github.com/fabriciotm/Servidor-Meganet-Telebradesco.git
cd Servidor-Meganet-Telebradesco
py -m venv .venv
.venv\Scripts\activate
py -m pip install --upgrade pip
pip install -r requirements.txt
py servidor_meganet_telebradesco.py --config meganet_config.json
```

Se o comando `py` não estiver disponível, use `python`.

Para encerrar o servidor, pressione `Ctrl+C`.

## Instalação no Linux

Em distribuições Debian/Ubuntu, se necessário:

```bash
sudo apt update
sudo apt install python3 python3-venv python3-pip
```

Depois:

```bash
git clone https://github.com/fabriciotm/Servidor-Meganet-Telebradesco.git
cd Servidor-Meganet-Telebradesco
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python servidor_meganet_telebradesco.py --config meganet_config.json
```

Também é possível iniciar pelo módulo:

```bash
python -m meganet_server --config meganet_config.json
```

## Primeiro acesso à interface web

Abra:

```text
http://127.0.0.1:19778
```

Credenciais iniciais:

```text
Usuário: admin
Senha:   admin
```

O arquivo `admin_web_users.json` é criado automaticamente no primeiro início. **Troque a senha `admin` antes de liberar a interface para outra máquina ou rede.**

Pela interface é possível administrar usuários MegaNet, conteúdo, correio, contas Telebradesco, configurações, estatísticas e histórico de acessos.

## Dados de demonstração

### MegaNet

O cadastro incluído usa:

```text
Perfil/bootstrap: DEMO
Login bootstrap:  MEGANET
Senha bootstrap:  DEMO123
Nome MegaNet:     DEMO
Senha MegaNet:    1234
Código de acesso: DEMO1234565A
E-mail:           demo@meganet.tectoy.com.br
```

A mailbox de `DEMO` já contém mensagens fictícias e o arquivo `meganet/content/content.json` possui conteúdo de demonstração para todos os grupos principais.

Dependendo do cartucho/emulador utilizado, o login bootstrap e o `#id` MegaNet aparecem em etapas distintas; por isso as duas credenciais estão listadas acima.

### Telebradesco

Conta fictícia incluída:

```text
Agência:      1234
Conta:        1234567
Dígito:       4
Senha:        4321
Data Cartão:  1508
```

O conteúdo financeiro está separado conforme o modelo atual:

- `telebradesco/accounts.json` — identificação, senha, bloqueio e histórico de acesso;
- `telebradesco/accounts_content.json` — saldos, extratos, lançamentos, investimentos, poupança e cartões.

## Portas padrão

| Serviço | Endereço padrão | Finalidade |
|---|---|---|
| MegaNet | `127.0.0.1:19777` | sessão MegaNet por TCP |
| Administração | `127.0.0.1:19778` | interface web |
| Telebradesco | `127.0.0.1:19779` | sessão Telebradesco por TCP |
| Adapter Telebradesco | `127.0.0.1:19780` | protocolo interno `TBA1/TBR1` |

Por segurança, a configuração de demonstração escuta apenas em `127.0.0.1`. Para aceitar conexões de outras máquinas, altere os hosts necessários para `0.0.0.0` em `meganet_config.json` ou pela interface administrativa e configure o firewall adequadamente.

## TCP, serial e modem físico

Os serviços podem ser usados por TCP, serial ou ambos. Os parâmetros principais ficam em `meganet_config.json`:

```json
{
  "meganet_transport": "tcp",
  "meganet_host": "127.0.0.1",
  "meganet_port": 19777,
  "meganet_serial_port": "COM12",
  "telebradesco_transport": "tcp",
  "telebradesco_host": "127.0.0.1",
  "telebradesco_port": 19779,
  "telebradesco_serial_port": "COM10"
}
```

No Linux, portas seriais normalmente aparecem como `/dev/ttyUSB0`, `/dev/ttyACM0` ou equivalentes. O usuário que executa o servidor precisa ter permissão para acessar o dispositivo.

Para ambientes com Asterisk, o projeto também implementa os protocolos internos `MGA1/MGR1` e `TBA1/TBR1`. Eles são interfaces entre o servidor e os módulos de telefonia; não fazem parte do protocolo original dos cartuchos. Consulte `PROTOCOLO.md`.

## Estrutura principal

```text
meganet_server/
  admin/          interface administrativa
  meganet/        protocolo, sessão, conteúdo, contas e correio MegaNet
  telebradesco/   protocolo, sessão, contas e conteúdo Telebradesco
  runtime/        listeners TCP/serial, autodetecção, estatísticas e lifecycle
meganet/
  cadastros.json
  content/content.json
  mail/
telebradesco/
  accounts.json
  accounts_content.json
meganet_config.json
```

## Configuração

A precedência é:

```text
opção explícita da linha de comando > meganet_config.json > valor padrão
```

Para consultar todas as opções:

```bash
python servidor_meganet_telebradesco.py --help
```

Para gerar uma nova configuração padrão em outro diretório:

```bash
python servidor_meganet_telebradesco.py --config minha_config.json --write-default-config
```

## Testes

Instale as dependências de desenvolvimento:

```bash
pip install -r requirements-dev.txt
```

Execute a suíte:

```bash
python -m pytest
```

Ou o quality gate completo:

```bash
python tools/quality_gate.py
```

A suíte inclui vetores de regressão dos protocolos MegaNet e Telebradesco, além de testes de transporte, persistência, concorrência, interface administrativa e recovery de I/O.

## Documentação técnica

- `PROTOCOLO.md` — protocolo MegaNet, Telebradesco e integrações internas com Asterisk;
- `API.md` — principais pontos de integração Python;
- `meganet_config.json` — configuração pronta para demonstração.


