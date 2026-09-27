"""Valida invariantes de um workflow n8n exportado em JSON.

Importar um workflow com conexão apontando para nó inexistente, ou para uma saída
que o nó não tem (índice 1 sem onError=continueErrorOutput), dá erro na importação
ou — pior — o workflow importa e a linha simplesmente nunca é processada.

    python3 tools/validar_workflow.py workflow-n8n.json
    python3 tools/validar_workflow.py workflow-n8n.v2.json
"""

from __future__ import annotations

import json
import sys
from collections import deque
from pathlib import Path


def saidas_nativas(no: dict) -> int:
    """Quantas saídas 'main' o nó declara por si (sem contar o ramo de erro).

    Switch/IF/SplitInBatches têm saídas fixas definidas pelos próprios parâmetros;
    todo o resto tem uma só.
    """
    tipo = no.get("type", "")
    parametros = no.get("parameters", {})
    if tipo.endswith(".switch"):
        regras = parametros.get("rules", {}).get("values", []) or []
        extra = 1 if parametros.get("options", {}).get("fallbackOutput") == "extra" else 0
        return max(1, len(regras)) + extra
    if tipo.endswith(".if") or tipo.endswith("splitInBatches"):
        return 2
    return 1


def validar(caminho: Path) -> tuple[list[str], list[str]]:
    wf = json.loads(caminho.read_text(encoding="utf-8"))
    nomes = {no["name"] for no in wf["nodes"]}
    erros: list[str] = []
    avisos: list[str] = []

    # 1. Toda conexão aponta para nó existente.
    for origem, saidas_por_tipo in wf.get("connections", {}).items():
        if origem not in nomes:
            erros.append(f"conexão de nó inexistente: {origem}")
        for tipo, saidas in saidas_por_tipo.items():
            if tipo != "main":
                avisos.append(f"{origem}: saída do tipo '{tipo}' (não validada)")
            for indice, alvos in enumerate(saidas):
                for alvo in alvos:
                    if alvo["node"] not in nomes:
                        erros.append(f"{origem}[{indice}] aponta para nó inexistente: {alvo['node']}")

    # 2. Saída usada existe de fato no nó.
    por_nome = {no["name"]: no for no in wf["nodes"]}
    for origem, saidas_por_tipo in wf.get("connections", {}).items():
        if origem not in por_nome:
            continue
        disponiveis = saidas_nativas(por_nome[origem])
        if por_nome[origem].get("onError") == "continueErrorOutput":
            disponiveis += 1
        for indice, alvos in enumerate(saidas_por_tipo.get("main", [])):
            if alvos and indice >= disponiveis:
                erros.append(
                    f"{origem}: conexão na saída {indice}, mas o nó só tem "
                    f"{disponiveis} saída(s) — falta onError=continueErrorOutput "
                    "ou a opção de saída extra?"
                )

    # 3. Todo nó (menos os de gatilho) tem entrada; todo nó é alcançável do gatilho.
    com_entrada = {
        alvo["node"]
        for saidas_por_tipo in wf.get("connections", {}).values()
        for saidas in saidas_por_tipo.get("main", [])
        for alvo in saidas
    }
    gatilhos = [
        no["name"] for no in wf["nodes"]
        if "trigger" in no["type"].lower() or no["type"].endswith("manualTrigger")
    ]
    for nome in sorted(nomes - com_entrada - set(gatilhos)):
        erros.append(f"nó sem entrada (nunca executa): {nome}")

    alcancaveis = set(gatilhos)
    fila = deque(gatilhos)
    while fila:
        atual = fila.popleft()
        for saidas in wf.get("connections", {}).get(atual, {}).get("main", []):
            for alvo in saidas:
                if alvo["node"] not in alcancaveis:
                    alcancaveis.add(alvo["node"])
                    fila.append(alvo["node"])
    for nome in sorted(nomes - alcancaveis):
        erros.append(f"nó não alcançável a partir do gatilho: {nome}")

    # 4. Ramos que terminam sem voltar para o SplitInBatches (a linha nunca é
    #    marcada, e o laço pode avançar sem que o ramo tenha terminado).
    loops = [no["name"] for no in wf["nodes"] if no["type"].endswith("splitInBatches")]
    if loops:
        conexoes = wf.get("connections", {})
        visitados: set = set()
        fila2 = deque(loops)
        while fila2:
            atual = fila2.popleft()
            for saidas in conexoes.get(atual, {}).get("main", []):
                for alvo in saidas:
                    if alvo["node"] not in visitados and alvo["node"] not in loops:
                        visitados.add(alvo["node"])
                        fila2.append(alvo["node"])
        for nome in sorted(visitados):
            if not any(conexoes.get(nome, {}).get("main", [])):
                avisos.append(f"ramo que termina sem voltar ao laço: {nome}")

    return erros, avisos


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    falhou = False
    for argumento in sys.argv[1:]:
        caminho = Path(argumento)
        erros, avisos = validar(caminho)
        print(f"=== {caminho.name} ===")
        for aviso in avisos:
            print(f"  aviso: {aviso}")
        for erro in erros:
            print(f"  ERRO: {erro}")
        if not erros and not avisos:
            print("  ok")
        falhou = falhou or bool(erros)
        print()
    return 1 if falhou else 0


if __name__ == "__main__":
    raise SystemExit(main())
