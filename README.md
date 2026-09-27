# Correção automática de entregas

Pipeline que recebe as entregas de uma planilha Google, baixa o ZIP do aluno,
roda análise estática e manda o código para o Gemini, encerrando com nota e
feedback gravados de volta na mesma linha da planilha.

```
Google Sheets (ENTREGAS)
   │  linhas com status_avaliacao = PENDENTE
   ▼
n8n ── Loop cada linha ── Checar Origem ──┬── Drive  ──► GET /download-zip
                                          └── GitHub ──► GET /download-github
                                                        │
                                     POST /check (multipart: file + ra + atividade)
                                                        │
                     ┌──────────────────────────────────┴─────────────┐
                     │  API (main.py)                                 │
                     │   • extrai o ZIP com limites e sem Zip Slip    │
                     │   • sintaxe de todos os .py (todos os erros)   │
                     │   • Ruff com a régua de ruff.toml              │
                     │   • métricas de autoria (triagem, não acusação)│
                     │   • hash canônico + bloco de contexto p/ LLM   │
                     └──────────────────────────────────┬─────────────┘
                                                        ▼
                                    Gemini (temperature 0) → nota + feedback
                                                        ▼
                                    Google Sheets (AVALIADO | FALHOU)
```

Arquivos: `main.py` (API), `ruff.toml` (régua de lint), `Dockerfile` +
`docker-compose.yml` (infra), `workflow-n8n.v2.json` (workflow novo),
`workflow-n8n.json` (o antigo, preservado), `tools/` (gerador, validador e testes
do workflow), `tests/` (testes da API).

## Subir o ambiente

```bash
cp .env.example .env      # revise as instruções sobre N8N_ENCRYPTION_KEY
docker compose up -d --build
docker compose ps         # os dois serviços devem ficar "healthy"
```

Primeiro acesso ao n8n: `http://localhost:5678` (agora só em localhost). Na
primeira subida o n8n pede a criação da conta do dono da instância.

Pinar a versão do n8n que já está rodando, para uma atualização não mudar o
comportamento no meio do semestre:

```bash
docker compose exec n8n n8n --version   # ex.: 1.114.4
echo 'N8N_TAG=1.114.4' >> .env && docker compose up -d n8n
```

Importar o workflow: no n8n, *Import from File* → `workflow-n8n.v2.json`. Ele
entra como uma cópia (id novo), então o antigo continua intacto para comparar.

## Rodar os testes

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt

.venv/bin/python -m pytest tests -q          # 30 testes da API
.venv/bin/ruff check main.py tests tools --config ruff.toml

python3 tools/validar_workflow.py workflow-n8n.v2.json   # invariantes do grafo
node tools/testar_parser_nota.js                          # parser da resposta do Gemini
```

`tools/derivar_workflow_v2.py` regenera o `workflow-n8n.v2.json` a partir do
original: edite as mudanças lá, não no JSON na mão.

## API v3.0.0 — o que mudou

Compatível com o workflow antigo: o endpoint, os campos `codigo_completo`,
`hash_codigo`, `analise_ia.nivel_suspeita` e `analise_resultado` continuam iguais.
Campos novos convivem com os antigos.

Correções de justiça na nota:

1. **Ruff que não roda não vira mais "nenhum aviso".** Antes, um `except Exception:
   return []` fazia o relatório afirmar que não havia avisos quando o linter tinha
   falhado — nota inflada em silêncio. Agora `ruff_status` é `ok | indisponivel |
   timeout | erro`, com o detalhe, e o texto enviado ao modelo diz explicitamente
   que o lint não rodou.
2. **O relatório do Ruff finalmente chega ao Gemini.** O prompt antigo mandava o
   modelo "considerar os avisos do linter fornecidos no relatório acima", mas o
   prompt só continha o código-fonte: o modelo era instruído a avaliar um dado que
   não tinha. Agora o nó manda `contexto_llm` (status da sintaxe, violações
   agrupadas por regra, ressalvas), gerado pela API.
3. **Todos os erros de sintaxe, com arquivo e linha.** Antes só o primeiro erro,
   sem dizer de qual arquivo veio (`sintaxe` continua sendo devolvido para
   compatibilidade, e `erros_sintaxe` traz a lista completa).
4. **Hash determinístico.** A ordem dos arquivos vinha do `os.walk` (arbitrária):
   a mesma entrega podia gerar `hash_codigo` diferente entre execuções. Agora os
   arquivos são ordenados por caminho. O algoritmo do hash não mudou — só a
   ordem. Hashes gravados antes desta versão podem não bater com os novos.

Outras correções:

5. **Zip Slip real.** A checagem antiga comparava prefixo de string
   (`str(destino).startswith(str(pasta))`), que aceita `<tmp>/extracted_evil/x.py`
   para `<tmp>/extracted`. Agora usa `Path.is_relative_to` sobre o caminho
   resolvido. Também são recusados/reportados: symlinks, separador `\` do Windows
   (normalizado), mais de `MAX_MEMBROS_ZIP` membros, mais de
   `MAX_DESCOMPACTADO_MB` extraídos e razão de compressão absurda (zip bomb).
6. **Limites de recurso.** Upload tem teto na leitura (não carrega tudo em
   memória), ruff tem timeout de 60 s, `codigo_completo` é truncado em 60 000
   caracteres com aviso explícito (`codigo_truncado`, `avisos`) e o modelo é
   instruído a não penalizar o que não conseguiu ver.
7. **Escopo do lint = escopo da análise.** O ruff roda sobre exatamente os `.py`
   que entraram na análise, não sobre a pasta inteira (evita lintar `.venv/` do
   aluno e demora proporcional).
8. **Contagem de comentários corrigida (era erro de triagem).** `_medir_generico`
   contava qualquer linha começando com `#` ou `*` como comentário: em CSS,
   `#fff` e `--minha-var` viravam comentário; em HTML, a segunda linha de um
   comentário multilinha não era contada. Agora há um scanner de verdade por
   linguagem (`//`, `/* */`, `<!-- -->`) que ignora o conteúdo de strings.
9. **Auditoria.** A resposta traz `versao_analisador`, `versao` da régua
   (hash do `ruff.toml` + limiares), `ruff_resumo` (contagem por regra),
   `criterios` (qual critério de triagem foi ou não atendido) e `metricas`.
10. **`GET /healthz`** e `X-API-Key` opcional (só exige se `API_TOKEN` estiver
    definido — o padrão continua aberto, como antes).

`ruff.toml` — o padrão do ruff roda apenas E4/E7/E9/F. A régua versionada aqui
soma W, B, C4, I e SIM, que pegam bug provável e código enganoso sem virar lista
de reclamações de estilo. Comprimento de linha (`E501`) fica de fora de propósito;
para ligar, veja a nota no fim do arquivo — e avise os alunos, porque a régua
muda.

## Workflow v2 — o que mudou

1. **`temperature: 0` no Gemini.** Era o padrão (1.0): a mesma entrega podia
   receber notas diferentes. *Confira na UI* se a opção `Temperature` aparece como
   0 nas Options do nó (o nome do parâmetro pode variar com a versão do n8n).
2. **Falha de parsing deixa de virar nota em branco.** Antes, qualquer erro de
   leitura da resposta gravava `nota = null` com status `AVALIADO`. Agora o nó
   Code lança erro, o item cai no ramo de erro e a linha é marcada `FALHOU` com o
   motivo. O extrator de JSON também ficou robusto: pega o primeiro objeto
   balanceado respeitando strings e escapes, em vez do regex
   `"feedback"\s*:\s*"([\s\S]*?)"\s*\}` que quebrava com aspas escapadas.
3. **Tratamento de erro em todos os pontos que podem falhar** (`Baixar do
   Drive`, `Baixar do GitHub`, `Buscar Enunciado`, `Baixar PDF Enunciado`,
   `Analisar Código (Ruff)`, `Avaliador Gemini`, `Separa Nota/Feedback`,
   `Salvar Nota/Feedback`, `Marcar Erro: Sem PDF`), cada um com 3 tentativas e
   espera de 5 s. Tudo converge para o nó novo `Marcar Falha Técnica`, que grava
   `status_avaliacao = FALHOU` e a mensagem do erro na coluna `feedback`, e volta
   para o laço.
4. **Linha sem link deixa de ser ignorada em silêncio.** O Switch `Checar Origem`
   ganhou a saída de fallback `sem_link` → nó `Marcar Erro: Sem Link` → `FALHOU`
   com motivo. Antes, uma linha `PENDENTE` sem `link_github`/arquivo não entrava
   em nenhuma saída e ficava pendente para sempre, sem sinal nenhum.
   *Confira na UI* se o Switch mostra 3 saídas, a terceira rotulada `sem_link`
   (é a opção "Fallback Output" nas Options do nó).
5. **`Marcar Erro: Sem PDF` deixa de ser beco sem saída.** Agora volta para a
   pausa e para o laço.
6. **Uma escrita só na planilha.** `Probabilidade IA` era um segundo update
   disparado em paralelo a partir do nó de análise — corria fora de hora e podia
   escrever depois de o laço já ter avançado. `prob_ia` passou para a mesma
   atualização que grava nota e feedback (e também para o ramo "sem PDF", que
   antes perdia a métrica).
7. **RA e atividade viajam até a API**, que os devolve no relatório. O
   `aluno_id`/`semestre` existiam no `/check` mas nunca eram enviados.
8. **Pausa de Segurança com valor explícito**: 4 s entre itens (era parâmetro
   vazio, ou seja, padrão implícito). Ajuste em `Pausa de Segurança` → 4 s atende
   os 15 RPM usuais do Gemini; para subir a vazão, reduza com cuidado.
9. **Timeout de 180 s no POST /check** — antes, uma análise travada prendia o
   item indefinidamente.

## Depois de importar: conferir na UI

Não dá para validar tudo isto sem abrir o n8n, porque parte depende do esquema de
parâmetros da sua versão:

- `Avaliador Gemini` → Options: temperatura em 0.
- `Checar Origem` → Options: Fallback Output = Extra Output, nome `sem_link`, com
  a terceira saída conectada em `Marcar Erro: Sem Link`.
- `Analisar Código (Ruff)` → Body: três campos — `file` (binário), `aluno_id` e
  `atividade` (texto). Os dois campos de texto usam expressão
  `$('Loop cada linha').item.json...`; se a sua versão do nó exigir outro tipo de
  parâmetro, o próprio n8n corrige ao abrir a UI.
- `Marcar Falha Técnica` e `Marcar Erro: Sem Link` → confira se a coluna `id`
  está como *matching column* e se a planilha tem as colunas `status_avaliacao`,
  `feedback` e `prob_ia`.

Teste em uma cópia da planilha com 2–3 linhas antes de rodar na turma. O workflow
v2 vem com `active: false` justamente para isso.

## Opcional: gravar as métricas da triagem para calibrar os limiares

Hoje só o rótulo (`prob_ia` = `SIM` ou vazio) vai para a planilha — a planilha não
tem coluna para as métricas. Para poder calibrar os limiares com dados reais,
crie estas colunas em `Entregas` e acrescente os mapeamentos no nó
`Salvar Nota/Feedback` (Columns → Values):

| Coluna nova         | Valor (expressão)                                                        |
|---------------------|--------------------------------------------------------------------------|
| `ia_densidade`      | `{{ $('Analisar Código (Ruff)').item.json.analise_ia.metricas.densidade_comentarios }}` |
| `ia_media_nomes`    | `{{ $('Analisar Código (Ruff)').item.json.analise_ia.metricas.media_comprimento_nomes }}` |
| `ia_linhas_codigo`  | `{{ $('Analisar Código (Ruff)').item.json.analise_ia.metricas.linhas_codigo }}` |
| `ruff_status`       | `{{ $('Analisar Código (Ruff)').item.json.ruff_status }}`                |
| `versao_regras`     | `{{ $('Analisar Código (Ruff)').item.json.versao_regras }}`              |

Com uma turma de dados, dá para escolher limiar olhando falso positivo e falso
negativo em vez de chutar. E `versao_regras` documenta sob qual régua cada nota
foi dada — útil se você precisar justificar uma nota meses depois.

## Armadilhas conhecidas

- **Tamanho do lote do laço.** O `Loop cada linha` está com 1 item por vez, e é
  isso que faz `$('Analisar Código (Ruff)').item` casar o código certo com o aluno
  certo. **Não aumente o batch size** sem trocar as referências entre nós por um
  Merge casando por `id`: com lote > 1, a nota de um aluno pode ser calculada com
  o código de outro.
- **`N8N_ENCRYPTION_KEY`.** Se o volume do n8n já existe, a chave atual está em
  `n8n_data/config.json` (`encryptionKey`) e precisa ir para o `.env` antes de
  qualquer recriação do container; trocar a chave com dados antigos torna as
  credenciais do Drive/Sheets irrecuperáveis.
- **A API não é mais publicada no host** (porta 8000). Para depurar do host:
  `docker compose run --rm -p 8000:8000 ruff-api`, ou descomente o mapeamento.
- **Triagem de autoria não é prova.** As métricas (densidade de comentários, média
  do comprimento de nomes) apontam suspeita estatística. Use como sinal para
  conversar, nunca como nota.
- **Windows no ZIP.** Entregas geradas no Windows trazem `\` como separador em
  alguns compactadores: são normalizadas e registradas em `avisos`.

## Backlog (não feito aqui)

- Detecção de cópia entre entregas da mesma turma (hoje só há `hash_codigo` por
  entrega; falta um índice de todos os hashes para comparar entre alunos).
- `responseSchema`/JSON mode no Gemini (depende do suporte do nó na sua versão do
  n8n; hoje o `temperature: 0` + parser tolerante cobrem o risco).
- Alerta fora do fluxo (e-mail/Slack) quando uma linha é marcada `FALHOU`.
- Autenticação obrigatória na API caso um dia ela precise ser exposta.
