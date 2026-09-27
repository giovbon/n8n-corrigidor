"""Gera workflow-n8n.v2.json a partir de workflow-n8n.json.

Por que um script: cada mudança fica registrada e a geração é reproduzível. O
workflow original NÃO é tocado — a v2 é um arquivo novo, para você importar no
n8n e testar em uma cópia antes de aposentar o antigo.

    python3 tools/derivar_workflow_v2.py

Mudanças aplicadas (ver README.md para o detalhamento e o que conferir na UI):
 1. Gemini com temperature 0 (a nota deixa de variar entre execuções).
 2. Prompt passa a receber `contexto_llm` (status da sintaxe, resumo do Ruff e
    alertas de truncamento) — antes ele mandava o modelo "considerar o relatório
    do Ruff" sem que o relatório estivesse no prompt.
 3. Nó Code de parsing: falha de leitura vira ERRO do nó (ramo de erro) em vez de
    nota nula gravada como "AVALIADO".
 4. RA e atividade enviados para a API (o relatório passa a ser auditável).
 5. retryOnFail nos nós do Google e no HTTP.
 6. Saída de fallback no Switch "Checar Origem" + nó "Marcar Erro: Sem Link".
 7. Tratamento de erro (onError=continueErrorOutput) nos pontos que podem falhar,
    com um nó único "Marcar Falha Técnica".
 8. "Probabilidade IA" deixa de ser uma segunda escrita paralela: prob_ia entra na
    mesma atualização da planilha que grava nota e feedback.
 9. Pausa de Segurança com valor explícito (antes: parâmetros vazios = padrão
    implícito).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, List

RAIZ = Path(__file__).resolve().parents[1]
ORIGEM = RAIZ / "workflow-n8n.json"
DESTINO = RAIZ / "workflow-n8n.v2.json"

NOME_LOOP = "Loop cada linha"
NOME_RUFF = "Analisar Código (Ruff)"
NOME_HTTP = "Analisar Código (Ruff)"
NOME_GEMINI = "Avaliador Gemini"
NOME_PARSER = "Separa Nota/Feedback"
NOME_SALVAR = "Salvar Nota/Feedback"
NOME_SEM_PDF = "Marcar Erro: Sem PDF"
NOME_PAUSA = "Pausa de Segurança"
NOME_SWITCH = "Checar Origem"

POS_SUFIXO = "={{ $('" + NOME_RUFF + "').item.json."


# ------------------------------------------------------------------------------
def carregar() -> Dict[str, Any]:
    return json.loads(ORIGEM.read_text(encoding="utf-8"))


def por_nome(wf: Dict[str, Any], nome: str) -> Dict[str, Any]:
    for no in wf["nodes"]:
        if no["name"] == nome:
            return no
    raise KeyError(f"nó não encontrado: {nome}")


def conectar(wf: Dict[str, Any], origem: str, destino: str, saida: int = 0) -> None:
    saidas = wf["connections"].setdefault(
        origem, {"main": [[]]}
    )["main"]
    while len(saidas) <= saida:
        saidas.append([])
    if not any(c["node"] == destino for c in saidas[saida]):
        saidas[saida].append({"node": destino, "type": "main", "index": 0})


def desconectar(wf: Dict[str, Any], origem: str, destino: str) -> None:
    for saidas in wf["connections"].get(origem, {}).get("main", []):
        saidas[:] = [c for c in saidas if c["node"] != destino]


def com_retry(no: Dict[str, Any]) -> Dict[str, Any]:
    no["retryOnFail"] = True
    no["maxTries"] = 3
    no["waitBetweenTries"] = 5000
    return no


def com_ramo_de_erro(no: Dict[str, Any]) -> Dict[str, Any]:
    no["onError"] = "continueErrorOutput"
    return no


def schema_com(schema: List[dict], coluna: str) -> List[dict]:
    """Garante que a coluna existe no schema do nó de atualização."""
    if any(item.get("id") == coluna for item in schema):
        return schema
    return schema + [{
        "id": coluna,
        "displayName": coluna,
        "required": False,
        "defaultMatch": False,
        "display": True,
        "type": "string",
        "canBeUsedToMatch": True,
        "removed": False,
    }]


def no_sheets(
    modelo: Dict[str, Any],
    *,
    nome: str,
    id_no: str,
    position: List[int],
    valores: Dict[str, str],
    ramo_de_erro: bool,
) -> Dict[str, Any]:
    """Cria um nó de update na planilha reaproveitando o cabeçalho do modelo."""
    no = {
        "parameters": copy.deepcopy(modelo["parameters"]),
        "id": id_no,
        "name": nome,
        "type": modelo["type"],
        "typeVersion": modelo.get("typeVersion", 4.7),
        "position": position,
        "credentials": copy.deepcopy(modelo.get("credentials", {})),
    }
    no["parameters"]["operation"] = "update"
    no["parameters"]["columns"]["mappingMode"] = "defineBelow"
    no["parameters"]["columns"]["matchingColumns"] = ["id"]
    no["parameters"]["columns"]["value"] = valores
    for coluna in ("id", "status_avaliacao", "feedback", "prob_ia", "nota"):
        no["parameters"]["columns"]["schema"] = schema_com(
            no["parameters"]["columns"]["schema"], coluna
        )
    com_retry(no)
    if ramo_de_erro:
        com_ramo_de_erro(no)
    return no


# ------------------------------------------------------------------------------
def aplicar(wf: Dict[str, Any]) -> Dict[str, Any]:
    modelo_sheets = por_nome(wf, NOME_SEM_PDF)
    limpador = por_nome(wf, NOME_PARSER)

    # (1) e (2) ------------------------------------------------------------
    gemini = por_nome(wf, NOME_GEMINI)
    gemini.setdefault("parameters", {}).setdefault("options", {})["temperature"] = 0
    gemini["parameters"]["text"] = PROMPT_AVALIADOR
    com_retry(gemini)
    com_ramo_de_erro(gemini)

    # (3) ------------------------------------------------------------------
    limpador["parameters"]["mode"] = "runOnceForEachItem"
    limpador["parameters"]["jsCode"] = CODIGO_PARSER
    com_ramo_de_erro(limpador)

    # (4) ------------------------------------------------------------------
    http = por_nome(wf, NOME_HTTP)
    http["parameters"]["bodyParameters"]["parameters"] = [
        {"parameterType": "formBinaryData", "name": "file", "inputDataFieldName": "data"},
        {
            "parameterType": "formData",
            "name": "aluno_id",
            "value": f"={{{{ $('{NOME_LOOP}').item.json.ra }}}}",
        },
        {
            "parameterType": "formData",
            "name": "atividade",
            "value": f"={{{{ $('{NOME_LOOP}').item.json.atividade }}}}",
        },
    ]
    http["parameters"].setdefault("options", {})["timeout"] = 180_000
    com_retry(http)
    com_ramo_de_erro(http)

    # (5) ------------------------------------------------------------------
    for nome in ("Buscar Entregas", "Buscar Enunciado", "Baixar do Drive",
                 "Baixar do GitHub", "Baixar PDF Enunciado"):
        com_retry(por_nome(wf, nome))

    # Nós cuja saída de erro é usada precisam de onError=continueErrorOutput,
    # senão a conexão do índice 1 fica pendurada e o workflow não importa direito.
    for nome in ("Baixar do Drive", "Baixar do GitHub", "Buscar Enunciado",
                 "Baixar PDF Enunciado"):
        com_ramo_de_erro(por_nome(wf, nome))

    # (6) ------------------------------------------------------------------
    switch = por_nome(wf, NOME_SWITCH)
    switch["parameters"]["options"] = {
        "fallbackOutput": "extra",
        "renameFallbackOutput": "sem_link",
    }
    sem_link = no_sheets(
        modelo_sheets,
        nome="Marcar Erro: Sem Link",
        id_no="b1f0c0de-0000-4000-8000-000000000001",
        position=[1840, -1184],
        valores={
            "id": f"={{{{ $('{NOME_LOOP}').item.json.id }}}}",
            "status_avaliacao": "FALHOU",
            "feedback": "Nenhum link de entrega preenchido (arquivo_zip_link ou link_github).",
        },
        ramo_de_erro=False,
    )
    wf["nodes"].append(sem_link)
    conectar(wf, NOME_SWITCH, "Marcar Erro: Sem Link", saida=2)
    conectar(wf, "Marcar Erro: Sem Link", NOME_PAUSA)

    # (7) ------------------------------------------------------------------
    falha = no_sheets(
        modelo_sheets,
        nome="Marcar Falha Técnica",
        id_no="b1f0c0de-0000-4000-8000-000000000002",
        position=[1840, -1008],
        valores={
            "id": f"={{{{ $('{NOME_LOOP}').item.json.id }}}}",
            "status_avaliacao": "FALHOU",
            "feedback": (
                "={{ ($json.error?.message || $json.error || 'erro sem detalhe')"
                ".toString().slice(0, 200) }}"
            ),
        },
        ramo_de_erro=False,
    )
    wf["nodes"].append(falha)

    for nome in (NOME_HTTP, "Baixar do Drive", "Baixar do GitHub", "Buscar Enunciado",
                 "Baixar PDF Enunciado", NOME_GEMINI, NOME_PARSER, NOME_SALVAR,
                 NOME_SEM_PDF):
        if nome != NOME_PARSER:
            com_retry(por_nome(wf, nome))
        com_ramo_de_erro(por_nome(wf, nome))
        conectar(wf, nome, "Marcar Falha Técnica", saida=1)
    conectar(wf, "Marcar Falha Técnica", NOME_PAUSA)
    conectar(wf, NOME_SEM_PDF, NOME_PAUSA)  # antes era um beco sem saída
    com_ramo_de_erro(por_nome(wf, NOME_SALVAR))

    # (8) ------------------------------------------------------------------
    salvar = por_nome(wf, NOME_SALVAR)
    salvar["parameters"]["columns"]["value"]["prob_ia"] = f"{POS_SUFIXO}analise_ia.nivel_suspeita }}}}"
    por_nome(wf, NOME_SEM_PDF)["parameters"]["columns"]["value"]["prob_ia"] = (
        f"{POS_SUFIXO}analise_ia.nivel_suspeita }}}}"
    )
    desconectar(wf, NOME_RUFF, "Probabilidade IA")
    wf["nodes"] = [n for n in wf["nodes"] if n["name"] != "Probabilidade IA"]
    wf["connections"].pop("Probabilidade IA", None)

    # (9) ------------------------------------------------------------------
    por_nome(wf, NOME_PAUSA)["parameters"] = {
        "resume": "timeInterval",
        "amount": 4,
        "unit": "seconds",
    }

    wf["name"] = "Correção automática de entregas (v2)"
    wf["versionId"] = "v2-derivado-localmente"
    wf["active"] = False
    return wf


PROMPT_AVALIADOR = """=Você é um professor e avaliador acadêmico de programação rigoroso, minucioso e imparcial.

Sua tarefa é avaliar a entrega do aluno com base em três fontes de informação:
1. O ENUNCIADO DA ATIVIDADE contido no arquivo PDF em anexo.
2. O BLOCO DE ANÁLISE AUTOMÁTICA (sintaxe + linter Ruff + ressalvas) reproduzido abaixo.
3. O CÓDIGO-FONTE DOS ARQUIVOS extraídos do projeto do aluno, reproduzido abaixo.

--- REGRA DE ESCOPO (ISOLAMENTO DE ATIVIDADE) ---
O projeto enviado pode conter códigos de exercícios ou entregas anteriores.
1. Identifique e avalie APENAS os arquivos, funções e testes que correspondem ao enunciado do PDF em anexo.
2. IGNORE completamente arquivos de exercícios passados ou código não solicitado na atividade atual.
3. APONTE no feedback que o aluno misturou códigos de outras atividades.

--- ANÁLISE AUTOMÁTICA DA ENTREGA ---
{{ $('Analisar Código (Ruff)').item.json.contexto_llm }}

--- CÓDIGO-FONTE DO ALUNO ---
{{ $('Analisar Código (Ruff)').item.json.codigo_completo }}

--- CRITÉRIOS DE AVALIAÇÃO ---
1. Requisitos Obrigatórios: verifique se o código atende ao que é pedido no PDF da atividade atual.
2. Qualidade de Código: use SOMENTE as ocorrências do Ruff listadas na análise automática acima. Se essa seção disser que o Ruff NÃO foi executado, não cite lint nenhum no feedback — não há dado para isso.
3. Correção Técnica e Testes: identifique bugs, falhas de sintaxe, erros de digitação (typos em strings de teste ou asserções) e nomes de métodos divergentes do especificado.
4. Pontuação: atribua uma nota numérica de 0.0 a 10.0 proporcional ao impacto dos acertos, erros e violações do linter encontrados no exercício em questão. Penalize coisas que mostrem que o código não rodará.
5. Limitações declaradas: se a análise automática avisar que o código foi TRUNCADO ou que parte dos arquivos não entrou na análise, NÃO penalize o aluno por conteúdo que você não consegue ver.

--- FORMATO DE SAÍDA ---
Responda ESTRITAMENTE em formato JSON válido contendo APENAS as chaves "nota" e "feedback". Não inclua texto fora do JSON e NÃO use marcadores Markdown (como ```json).

{
  "nota": 8.5,
  "feedback": "Feedback objetivo de até 3 frases (máximo 80 palavras) destacando os acertos, falhas exatas e apontamentos relevantes do linter sobre o exercício atual."
}"""


CODIGO_PARSER = r"""// Extrai nota e feedback da resposta do Gemini.
//
// Na v1, qualquer falha de parsing resultava em nota = null e feedback vazio, que
// eram gravados na planilha com status "AVALIADO" — nota em branco passando por
// correção concluída. Agora, falha de leitura = erro do nó, que o workflow manda
// para o ramo de erro (status FALHOU + motivo).

const conteudo = $input.item.json?.content;
const partes = conteudo?.parts;

if (!Array.isArray(partes) || partes.length === 0 || typeof partes[0]?.text !== 'string') {
  throw new Error(
    'Resposta do Gemini em formato inesperado: ' + JSON.stringify(conteudo).slice(0, 300),
  );
}

let texto = partes[0].text.trim();
texto = texto.replace(/^```(?:json)?/i, '').replace(/```$/, '').trim();

// Pega o primeiro objeto JSON balanceado, respeitando strings e escapes: o
// modelo às vezes escreve uma frase antes do JSON, e o regex anterior
// (`"feedback"\s*:\s*"([\s\S]*?)"\s*\}`) quebrava com aspas escapadas.
function extrairObjetoJson(str) {
  const inicio = str.indexOf('{');
  if (inicio === -1) return null;
  let profundidade = 0;
  let emString = false;
  let escape = false;
  for (let i = inicio; i < str.length; i++) {
    const c = str[i];
    if (emString) {
      if (escape) { escape = false; continue; }
      if (c === '\\') { escape = true; continue; }
      if (c === '"') { emString = false; }
      continue;
    }
    if (c === '"') { emString = true; continue; }
    if (c === '{') profundidade++;
    else if (c === '}') {
      profundidade--;
      if (profundidade === 0) return str.slice(inicio, i + 1);
    }
  }
  return null;
}

let dados = null;
try {
  dados = JSON.parse(texto);
} catch (e) {
  const bruto = extrairObjetoJson(texto);
  if (bruto) {
    try { dados = JSON.parse(bruto); } catch (e2) { dados = null; }
  }
}

if (!dados || typeof dados !== 'object') {
  throw new Error('Não foi possível interpretar a resposta do Gemini como JSON: ' + texto.slice(0, 300));
}

const notaBruta = typeof dados.nota === 'string' ? dados.nota.replace(',', '.') : dados.nota;
const nota = Number(notaBruta);
if (!Number.isFinite(nota)) {
  throw new Error('Resposta sem valor numérico em "nota": ' + JSON.stringify(dados).slice(0, 300));
}

let feedback = dados.feedback;
if (typeof feedback !== 'string' || feedback.trim() === '') {
  feedback = '(o avaliador não retornou feedback textual)';
}

return {
  json: {
    nota: Math.max(0, Math.min(10, Math.round(nota * 10) / 10)),
    feedback: feedback.trim(),
  },
};"""


def main() -> None:
    wf = aplicar(carregar())
    DESTINO.write_text(json.dumps(wf, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"gerado: {DESTINO}")
    print(f"nós: {len(wf['nodes'])}")
    for no in wf["nodes"]:
        marca = []
        if no.get("retryOnFail"):
            marca.append("retry")
        if no.get("onError"):
            marca.append("ramo-de-erro")
        print(f"  - {no['name']}{' (' + ', '.join(marca) + ')' if marca else ''}")


if __name__ == "__main__":
    main()
