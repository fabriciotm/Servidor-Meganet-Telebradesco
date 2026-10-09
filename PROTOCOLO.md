# PROTOCOLO — MegaNet e Telebradesco

## 1. Escopo

Este documento descreve o protocolo implementado pelo Servidor MegaNet / Telebradesco. Ele distingue três camadas:

1. **protocolo da aplicação/ROM** — bytes e tokens efetivamente trocados com os cartuchos;
2. **transporte** — TCP, serial e a ponte de modem físico;
3. **protocolos internos dos módulos Asterisk** — `MGA1/MGR1` e `TBA1/TBR1`, que não fazem parte do protocolo original dos cartuchos.

Caminhos mantidos apenas por compatibilidade são indicados explicitamente quando relevantes.

---

# Parte I — MegaNet

## 2. Visão geral da sessão MegaNet

Uma sessão MegaNet possui duas fases principais:

1. **bootstrap textual/binário curto**, anterior ao protocolo de pacotes;
2. **camada de pacotes MegaNet**, usada para autenticação `#id`, cadastro, grupos de conteúdo, correio e troca de senha.

No caminho de modem físico moderno, o listener unificado pode receber antes disso o marcador interno `MGA1` do `app_meganet`. O servidor reserva a conexão e **não inicia o login** até receber `MGR1`, que indica que a ponte Asterisk considerou o enlace apto para a fase de dados. Se `MGR1` não chegar dentro do timeout, a conexão é fechada silenciosamente. `MGA1/MGR1` é, portanto, um protocolo **servidor ↔ módulo Asterisk**, não um token da ROM.

## 3. Bootstrap MegaNet

Fluxo validado em modo online:

```text
Servidor -> "login:\r"
ROM      -> <bootstrap_login> "\r"
Servidor -> "password:\r"
ROM      -> <bootstrap_password> "\r"
ROM      -> <profile/name>            (sem CR no fluxo observado)
Servidor -> ACK 0x06
ROM      -> DLE 0x10
Servidor -> ACK 0x06
Servidor -> pacote "#bemvindo#"
Servidor -> pacote "#ready#\r"
```

O `bootstrap_login`, a senha de bootstrap e o campo de profile pertencem à camada de acesso inicial. A autenticação principal da conta MegaNet ocorre posteriormente pelo token `#id`.

### 3.1 Proteção contra ruído

Durante os estados textuais (`WAIT_LOGIN`, `WAIT_PASSWORD`, `WAIT_PROFILE_FIELD`) são aceitos CR/LF, TAB e ASCII imprimível. Bytes incompatíveis podem ser descartados dentro da tolerância configurada; corrupção persistente encerra o TCP sem produzir mais respostas. Após a transição confiável para a camada de pacotes, esse guard deixa de controlar a sessão.

### 3.2 Hayes que vaza para a conexão

`+++` é tratado como sequência de escape e `ATH`/`ATH0` pode encerrar a conexão quando configurado para isso. Essa lógica existe porque, em algumas pontes, comandos destinados ao modem local podem atravessar o socket de dados.

## 4. Framing MegaNet

### 4.1 Servidor → ROM

```text
+------+-----+-----+---------+-------------+-------------+
| STX  | LEN | SEQ | PAYLOAD | CHECKSUM_HI | CHECKSUM_LO |
+------+-----+-----+---------+-------------+-------------+
  0x02   1B    1B    LEN B       1B            1B
```

Não existe ETX ao final nesse sentido.

Restrições implementadas:

- `LEN <= 127`;
- `SEQ` é um byte e circula em módulo 256;
- checksum de 16 bits:

```text
CHECKSUM = (LEN + SEQ + soma(PAYLOAD)) & 0xFFFF
```

### 4.2 ROM → servidor

```text
+------+-----+-----+---------+-------------+-------------+------+
| STX  | LEN | SEQ | PAYLOAD | CHECKSUM_LO | CHECKSUM_HI | ETX  |
+------+-----+-----+---------+-------------+-------------+------+
  0x02   1B    1B    LEN B       1B            1B        0x03
```

A ordem dos bytes do checksum é deliberadamente diferente da direção servidor → ROM.

### 4.3 ACK/NAK da camada de pacotes

Controles crus:

```text
ACK = 0x06
NAK = 0x15
```

O servidor mantém uma fila separada para ACK/NAK enquanto há um pacote servidor → ROM pendente, sem retirar bytes `0x06`/`0x15` que pertençam ao interior de um frame iniciado por STX.

No modo confiável stop-and-wait:

1. o servidor transmite um frame com `SEQ = N`;
2. aguarda ACK cru;
3. se recebe ACK, confirma e incrementa `SEQ`;
4. se recebe NAK ou timeout, retransmite **o mesmo frame com o mesmo SEQ**;
5. após o número máximo de tentativas, fecha o transporte (fail-closed).

Os downloads de grupos usam entrega confiável. Algumas respostas terminais de erro utilizam explicitamente `wait_ack=False`, porque a ROM não confirma esses pacotes com a mesma cadência dos downloads normais.

## 5. Autenticação principal MegaNet

Depois de `#bemvindo#/#ready#`, a ROM envia:

```text
#id <nome_meganet> <senha> [serviço]#\r
```

Exemplo observado:

```text
#id jose 123ABC SSO#\r
```

A validação usa:

- `fields.nome_meganet` como identificador da conta;
- `fields.senha` como senha;
- por padrão, comparação de senha case-insensitive, compatível com o comportamento observado da ROM.

A chave JSON interna do cadastro não é usada como fallback de login quando diverge de `fields.nome_meganet`.

## 6. Cadastro

Fluxo de aplicação:

```text
ROM      -> #get cadastro#\r
Servidor -> #ready#\r
ROM      -> #begin cadastro#\r
ROM      -> #item#\r / valores dos campos
ROM      -> #end cadastro#\r
Servidor -> #message begin#\r
Servidor -> linhas da mensagem
Servidor -> #message end#\r
Servidor -> #ready#\r
ROM      -> #bye#\r
```

Os campos recebidos são associados sequencialmente à tabela `FIELD_NAMES`. O cadastro persistente fica em `meganet/cadastros.json` por padrão.

Há validação específica para duplicidade de `nome_meganet` e de `codigo_acesso`.

## 7. Mensagens para a ROM

Um bloco de mensagem normal é:

```text
#message begin#\r
<linha 1>\r
<linha 2>\r
...
#message end#\r
#ready#\r
```

Linhas visíveis são mantidas em até **35 caracteres**, limite conservador validado para evitar wrap automático de 36 caracteres seguido de avanço adicional pelo CR.

O marcador editorial `<BR>` (também são reconhecidos `[BR]` e `{BR}`) representa uma linha em branco e é convertido em um CR vazio na transmissão.

## 8. Grupos de conteúdo

Roteamento padrão:

```text
#send news 1# -> TECTOY NEWS / DICAS / SEGA CLUB
#send news 2# -> INTERNET
#send news 3# -> REVISTA
#send news 4# -> OUTROS / HOME SHOPPING
```

Resposta:

```text
ROM      -> #send news N#
Servidor -> #begin news N#\r
Servidor -> <dados do item 0> + #item#\r
Servidor -> <dados do item 1> + #item#\r
...
Servidor -> <dados do último item> + #end#\r
Servidor -> #ready#\r
```

Um item longo pode ocupar vários pacotes; somente o último chunk do item recebe `#item#` ou `#end#`.

Restrições atuais:

- payload de um pacote <= 127 bytes;
- até 50 linhas lógicas normalizadas por item no caminho estendido;
- orçamento conservador de **0x3F00 (16.128 bytes)** por grupo, reservando margem abaixo do buffer observado de aproximadamente `0x4000`.

## 9. Correio eletrônico MegaNet

Tokens principais:

```text
#get mail#
#send mail#
#begin mail#
#sep# / #item#
#end mail#
```

O servidor captura os payloads originais em Latin-1 entre `#begin mail#` e `#end mail#`. O parser reconhece rótulos em português/inglês e também o formato sem rótulos (destinatário, assunto e corpo).

O domínio interno padrão é:

```text
<nome_meganet>@meganet.tectoy.com.br
```

Após download bem-sucedido, a caixa postal pendente pode ser esvaziada e arquivada. Um contador de aviso de uma conexão preserva a indicação de nova mensagem quando a própria sessão já consumiu a mensagem recém-entregue.

## 10. Troca de senha MegaNet

Token dedicado:

```text
#senha <usuario> <senha_antiga> <senha_nova>#\r
```

A senha antiga é comparada à senha do cadastro. Quando a aplicação de troca está habilitada, `fields.senha` é atualizado e a ROM recebe um bloco de mensagem de sucesso. Erros de usuário/senha são devolvidos em mensagem de aplicação.

## 11. Encerramento

`#bye#` dispara persistência pendente e encerramento da transação. `ATH/ATH0` também pode encerrar a sessão quando chega pelo caminho Hayes/vazamento de modem.

---

# Parte II — Telebradesco

## 12. Visão geral

O Telebradesco **não usa o bootstrap `login:/password:` do MegaNet**. Quando a conexão de dados está estabelecida, o servidor permanece silencioso até a ROM iniciar o diálogo Class‑1.

O fluxo implementado utiliza comandos Hayes de fax Class‑1, um quadro de login HDLC/T.30 com payload `MEGABRA...`, confirmação binária e depois transferência de dados em quadros de recepção (`AT+FRM=48`).

## 13. Sequência Class‑1 implementada

Fluxo simplificado:

```text
ROM      -> AT+FRH=3\r
Servidor -> 1\r 10 03 0\r                 (FRH inicial vazio)

ROM      -> AT+FTH=3\r
Servidor -> 1\r
ROM      -> <quadro de login bit-revertido, final DLE/ETX>
Servidor -> 1\r                           (CONNECT/result após login)

ROM      -> <confirmação binária FF C8 C1 ... bit-revertida>
Servidor -> 0\r

ROM      -> AT+FRM=48\r
Servidor -> 1\r + <preâmbulo>              (hello)

ROM      -> AT+FTH=3\r
Servidor -> 1\r
ROM      -> <ACK/NAK de 3 bytes>
Servidor -> 0\r

ROM      -> AT+FRM=48\r
Servidor -> 1\r + <quadro de dados Class-1>

ROM      -> AT+FTH=3\r
Servidor -> 1\r
ROM      -> <ACK/NAK de 3 bytes>
Servidor -> 0\r

... repete para os demais quadros ...
```

Ao terminar, `AT+FRM=48` sem quadro pendente recebe `3\r`. `ATH/ATH0` recebe `0\r` e encerra a sessão; determinados erros de autenticação podem aplicar um hold configurável antes desse OK final para permitir que a ROM apresente a mensagem.

## 14. Bit reversal e DLE

No caminho implementado, os **bytes binários recebidos da ROM** nos quadros de login, confirmação e ACK/NAK chegam na representação bit-revertida observada e são normalizados pelo servidor com reversão de bits dentro de cada byte (bit 7↔0, 6↔1, etc.). Os quadros de dados produzidos por `_tele_class1_wire` são, por sua vez, emitidos pelo servidor no formato DTE construído pela própria função; este documento não atribui ao servidor uma segunda reversão desses bytes de saída.

No login, após reverter os bits recebidos, o terminador esperado é:

```text
DLE ETX = 10 03
```

DLE no conteúdo usa stuffing: um DLE literal aparece duplicado (`10 10`).

No fio bit-revertido, o terminador aparece como `08 C0`; a detecção exige um `08` não pareado imediatamente antes do `C0`, para não finalizar em um byte `C0` isolado do payload.

## 15. Quadro de login real HDLC/T.30

Após bit reversal e remoção do `DLE ETX`, o formato confirmado é:

```text
FF C0 C2 + ASCII("MEGABRA...")
```

Também é aceito:

```text
FF C8 C2 + ASCII("MEGABRA...")
```

`C8` é tratado como variante com final bit no byte de controle.

O FCS HDLC/T.30 não é calculado pelo servidor nesta camada: no caminho físico ele pertence ao modem/transporte.

### 15.1 Formato sintético legado

Existe ainda um decoder compatível com versões antigas de teste:

```text
10 TAG SEQ LEN PAYLOAD CHECKSUM_HI CHECKSUM_LO
```

Esse formato não é descrito como o formato original da ROM; permanece apenas para compatibilidade com captures/testes históricos.

## 16. Payload MEGABRA

Formato aceito:

```text
MEGABRA + AGENCIA(4) + CONTA(7) + DV(1) + SENHA(4) + RESTANTE(8)
```

Logo após `MEGABRA`, portanto, existem 24 dígitos no formato atualmente validado.

Os últimos oito dígitos são mantidos como campo `restante`. O **Data Cartão** é exclusivamente os **quatro últimos dígitos (DDMM)** desse campo. Os quatro anteriores são mantidos apenas como prefixo diagnóstico.

`0000` significa ausência de seleção de cartão.

## 17. Confirmação binária do login

Após o login pode existir um prefixo lógico antes do frame de confirmação. O servidor procura a sequência completa de nove bytes:

```text
FF C8 C1 00 52 13 01 01 00
```

Capturas podem trazer, por exemplo, o prefixo lógico:

```text
00 00 10 41
```

O servidor não retorna ao modo de comandos até consumir o frame `FF C8 C1` completo; bytes restantes são reenviados ao parser normal.

## 18. Conteúdo financeiro — stream lógico

A compilação constrói um único stream de seções. Ordem atual:

```text
5 -> 2 -> 3 -> 1 -> 6 -> 8 -> 9 -> A -> 0 -> status0 -> status1 -> status2
```

A primeira seção começa por:

```text
01 <tipo ASCII>
```

As seguintes usam marcador:

```text
1D 03 <tipo ASCII>
```

Assim, `marker("0")` é:

```text
1D 03 30
```

### 18.1 Semântica das seções

| Seção | Conteúdo implementado |
|---|---|
| `5` | Conta Corrente: título de 32 B, texto auxiliar 28 B, data 6 B, saldo 20 B e extrato |
| `2` | saldos da Conta Corrente + lista de investimentos; quarto campo de 18 B é reservado/em branco |
| `3` | Lançamentos Futuros, registros de transação |
| `1` | Últimos Lançamentos/summary da Conta Corrente, até 3 registros de 26 B |
| `6` | cartão selecionado por Data Cartão; omitida se não houver seleção exata |
| `8` | extrato da Poupança (estrutura tipo 5) |
| `9` | saldos da Poupança (estrutura tipo 2) |
| `A` | resumo da Poupança + últimos lançamentos |
| `0` | marcador de término das seções, antes dos três estados |

### 18.2 Registro de transação

Layout usado por `_tele_transaction`:

```text
DATA(4) + HISTORICO(15) + DOCUMENTO(7) + VALOR(19, alinhado à direita)
```

Total: 45 bytes antes da compressão.

### 18.3 Resumo de 26 bytes

Seções de resumo usam:

```text
DATA(6) + VALOR(20, alinhado à direita)
```

São normalizados até três registros.

### 18.4 Cartão — seção 6

Cabeçalho:

```text
TITULO(32) + 3 espaços + 23 espaços
```

Cada compra:

```text
"@@@" + DATA(16) + DESCRICAO(28) + VALOR(20)
```

A seção só existe quando os quatro dígitos DDMM recebidos no login coincidem exatamente com um cartão registrado.

## 19. Compressão do stream Telebradesco

A compressão é uma RLE específica apenas para sequências de espaço (`0x20`) e caractere zero ASCII (`0x30`).

Para runs de 2 a 15 bytes:

```text
espaços: 0x90 | quantidade
zeros:   0x80 | quantidade
```

Um único espaço/zero permanece literal. Runs maiores são quebrados em blocos de no máximo 15.

## 20. Trailer de status

Após `marker("0")` são enviados exatamente **três registros de status**, cada um:

```text
01 30 D1 D2 D3 04 00
```

onde `D1D2D3` são três dígitos ASCII.

Registro de sucesso:

```text
01 30 39 39 39 04 00
```

isto é, código `999`.

Códigos atualmente mapeados:

| Situação | Código lógico |
|---|---:|
| sucesso | `999` |
| login malformado / base indisponível | `001` |
| senha ausente | `010` |
| senha inválida | `011` |
| conta bloqueada | `020` |
| agência incorreta | `040` |
| conta inexistente | `042` |

### 20.1 Slots

- sucesso: `999 / 999 / 999`;
- bloqueio nativo `020`: `020 / 020 / 999`;
- agência incorreta `040`: `040 / 040 / 040`;
- conta inexistente `042`: `042 / 042 / 042`.

No modo nativo de bloqueio, as seções pertinentes são omitidas para que a ROM use o slot de status correspondente e apresente sua própria mensagem (`C/C BLOQUEADA`, etc.).

## 21. Divisão em quadros V.27/Class‑1

O stream lógico é dividido em unidades de até **120 bytes úteis**. Cada payload armazenado em `frames` recebe ainda **um espaço `0x20` terminal**, portanto possui no máximo **121 bytes**.

Os limites de frame são deslocados para trás quando necessário para nunca cortar no meio de:

- marcador de seção;
- registro de status de 7 bytes.

Isso é importante porque esses elementos são tratados como átomos pelo parser da ROM.

## 22. Wire de um quadro Class‑1 de dados

Antes de bit reversal do modem, o servidor constrói:

```text
10 TAG SEQ LEN PAYLOAD CHECKSUM_HI CHECKSUM_LO 33 0D
```

Valores usuais:

- byte inicial: `0x10`;
- `TAG`: `0x80`;
- `SEQ`: inicia em 2;
- `LEN`: 1 byte;
- checksum: `sum(10 TAG SEQ LEN PAYLOAD) & 0xFFFF`, big-endian;
- bytes `0x10` após o primeiro byte são duplicados (DLE stuffing);
- terminador textual acrescentado: `"3\r"` (`33 0D`).

A sessão envia esse wire precedido pelo resultado `1\r` quando atende `AT+FRM=48`.

## 23. ACK/NAK Telebradesco

Após `AT+FTH=3`, a ROM envia três bytes binários; o servidor reverte os bits e lê o último byte lógico:

```text
0x21 -> ACK: avança SEQ e frame_index
0x22 -> NAK/retry: mantém o mesmo SEQ e o mesmo frame
```

Essa preservação de sequência no retry é obrigatória: retransmitir como novo `SEQ` cria um buraco no estado do receptor.

---

# Parte III — Protocolos internos de integração

## 24. Listener TCP unificado

O mesmo endpoint TCP pode reconhecer os dois serviços e os adapters internos.

Indicadores fortes de Telebradesco incluem comandos Class‑1 (`+FCLASS`, `+FRH`, `+FTH`, `+FRM`), `ATX3DT` e `MEGABRA`.

Indicadores MegaNet incluem tokens `#ID`, `#SEND NEWS`, `#GET MAIL`, `#BEGIN MAIL`, `#BEMVINDO#` e a discagem observada `ATX3DP`.

Um prefixo Hayes incompleto, como apenas `A`, não é classificado precocemente como MegaNet. No timeout ambíguo, a implementação preserva o caminho Telebradesco para evitar que um `login:\r` espontâneo contamine o diálogo Class‑1.

## 25. `MGA1/MGR1` — app_meganet

Esses marcadores pertencem à integração com o módulo Asterisk:

```text
app_meganet -> servidor: MGA1
servidor: reserva o socket e aguarda
app_meganet -> servidor: MGR1
servidor: inicia imediatamente o bootstrap MegaNet online
```

Após `MGA1`, ausência de `MGR1` é tratada de forma estrita: fechamento sem fallback para `login:`.

## 26. `TBA1/TBR1` — app_telebradesco

O adapter binário evita duplicar no módulo C a lógica de contas/conteúdo financeiro.

### 26.1 Request

Network byte order:

```text
struct !4sBBH
MAGIC     = "TBA1"
VERSION   = 1
FLAGS     = 0
LOGIN_LEN = uint16
LOGIN     = LOGIN_LEN bytes ASCII (MEGABRA...)
```

Limite atual de login: 96 bytes.

### 26.2 Response

Header:

```text
struct !4sBBHIHH
MAGIC           = "TBR1"
VERSION         = 1
RESULT          = uint8
FRAME_COUNT     = uint16
STREAM_SIZE     = uint32
ACCOUNT_KEY_LEN = uint16
STATUS_LEN      = uint16
```

Em seguida:

```text
ACCOUNT_KEY
STATUS_TEXT
repetido FRAME_COUNT vezes:
    FRAME_LEN (uint16, network byte order)
    FRAME_PAYLOAD
```

`FRAME_PAYLOAD` é o mesmo payload lógico de até 121 bytes que será encapsulado pelo módulo Asterisk no transporte físico Class‑1.

Resultados de erro usam o mesmo header, com `FRAME_COUNT=0`, `STREAM_SIZE=0` e texto de status (`bad_header`, `bad_request`, `accounts_unavailable`, etc.).

---

# Parte IV — Persistência relacionada ao protocolo

## 27. MegaNet

Arquivos principais:

```text
meganet/cadastros.json
meganet/content/content.json
meganet/mail/...
meganet/password/...
```

A persistência é implementação do servidor e não faz parte do protocolo da ROM.

## 28. Telebradesco

Identificação e conteúdo financeiro são separados:

```text
telebradesco/accounts.json
telebradesco/accounts_content.json
```

`accounts.json` guarda identidade/metadados operacionais; `accounts_content.json` guarda saldos, extratos, investimentos, poupança e cartões.

---

---

# Parte V — Invariantes de implementação

Os testes de regressão preservam as seguintes propriedades:

1. framing, checksum e avanço de `SEQ` MegaNet;
2. ACK/NAK e retransmissão stop-and-wait sem alterar o frame retransmitido;
3. tokens e limites de conteúdo consumidos pela ROM;
4. bit reversal, DLE stuffing, terminador e códigos de status Telebradesco;
5. gramática e limites do stream financeiro Telebradesco;
6. protocolos internos `MGA1/MGR1` e `TBA1/TBR1`;
7. separação entre identidade (`accounts.json`) e conteúdo financeiro (`accounts_content.json`);
8. timings operacionais validados, salvo quando alterados explicitamente como mudança de protocolo.

## Vetores de regressão

A suíte em `tests/` fixa vetores conhecidos para os codecs e fluxos de sessão. Mudanças em bytes de wire, status, checksums ou timings devem ser acompanhadas de testes que documentem deliberadamente o novo comportamento.

## Organização da implementação

- `meganet_server/meganet/` contém o domínio MegaNet;
- `meganet_server/telebradesco/` contém o domínio Telebradesco;
- `meganet_server/runtime/` contém listeners e transportes;
- `meganet_server/admin/` contém exclusivamente a interface administrativa;
- `meganet_server/api.py` é a fachada recomendada para integrações novas.
