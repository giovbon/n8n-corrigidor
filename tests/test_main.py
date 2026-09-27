"""Testes da API de análise.

Foco nos pontos onde a v2.3.0 dava resultado errado em silêncio:
  * Zip Slip aceito por checagem de prefixo de string;
  * contagem de comentários que tratava "#fff" (CSS) como comentário;
  * ruff ausente relatado como "nenhum aviso";
  * só o primeiro erro de sintaxe, sem dizer de qual arquivo;
  * hash dependente da ordem (arbitrária) do os.walk.
"""

import hashlib
import io
import shutil
import zipfile

import pytest
from fastapi.testclient import TestClient

import main

cliente = TestClient(main.app)


# ------------------------------------------------------------------------------
# auxiliares
# ------------------------------------------------------------------------------
def zip_em_memoria(arquivos: dict, extras=None) -> bytes:
    """Monta um ZIP em memória. `arquivos`: {caminho: conteúdo}."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        for caminho, conteudo in arquivos.items():
            z.writestr(caminho, conteudo)
        for info, conteudo in (extras or []):
            z.writestr(info, conteudo)
    return buffer.getvalue()


def entregar(conteudo_zip: bytes, nome="entrega.zip", **campos):
    return cliente.post(
        "/check",
        files={"file": (nome, conteudo_zip, "application/zip")},
        data=campos,
    )


CODIGO_OK = """\
import os


def calcular_media(valores_da_turma):
    \"\"\"Calcula a média de uma lista de notas.\"\"\"
    total_acumulado = 0
    for nota_individual in valores_da_turma:
        total_acumulado += nota_individual
    return total_acumulado / len(valores_da_turma)
"""


# ------------------------------------------------------------------------------
# contagem de comentários (era o que mais distorcia a triagem de autoria)
# ------------------------------------------------------------------------------
def test_css_nao_trata_cor_e_variavel_como_comentario():
    css = "#fff { cor: red; }\nbody { --principal: #333; }\n"
    medidas = main._medir_generico(css, ".css")
    assert medidas.comentarios == 0, "cor (#fff) e custom property (--x) viraram comentário"


def test_css_conta_bloco_multilinha():
    css = "a { cor: red; }\n/* explica\n   em duas linhas */\nb { cor: blue; }\n"
    assert main._medir_generico(css, ".css").comentarios == 2


def test_js_conta_comentario_de_linha_e_bloco():
    js = 'const url = "http://exemplo.com"; // comenta\n/* abre\nfecha */\nconst x = 1;\n'
    assert main._medir_generico(js, ".js").comentarios == 3


def test_html_conta_comentario_multilinha():
    html = "<div>oi</div>\n<!-- comenta\nem duas linhas -->\n<p>x</p>\n"
    assert main._medir_generico(html, ".html").comentarios == 2


def test_python_conta_comentario_e_docstring():
    codigo = (
        "def calcular_media(valores):\n"
        '    """Soma e divide."""\n'
        "    # passo intermediário\n"
        "    total = sum(valores)\n"
        "    return total / len(valores)\n"
    )
    medidas = main._medir_python(codigo)
    assert medidas.comentarios == 2  # docstring da função + comentário
    assert medidas.nomes >= 3


def test_python_nao_conta_docstring_de_modulo():
    """Comportamento herdado da v2.3.0, mantido de propósito.

    Só docstring de função/classe entra na conta. Docstring de módulo fica de fora,
    o que SUBESTIMA a densidade — e errar para menos, aqui, significa marcar menos
    gente injustamente. Ver o docstring de `_medir_python`.
    """
    medidas = main._medir_python('"""Doc do módulo."""\nvalor = 1\n')
    assert medidas.comentarios == 0


# ------------------------------------------------------------------------------
# hash
# ------------------------------------------------------------------------------
def test_hash_normalizado_mantem_o_algoritmo_da_v230():
    """Trava o formato gravado na planilha: mudar isso invalida o histórico."""
    esperado = hashlib.sha256("a = 1\nb = 2".encode()).hexdigest()[:16]
    assert main.calcular_hash_normalizado(["a = 1\n\n  b = 2\n"]) == esperado


# ------------------------------------------------------------------------------
# extração do ZIP
# ------------------------------------------------------------------------------
def test_zip_slip_com_ponto_ponto(tmp_path):
    alvo = tmp_path / "extracted"
    alvo.mkdir()
    zip_path = tmp_path / "malicioso.zip"
    zip_path.write_bytes(zip_em_memoria({"../evil.py": "print(1)"}))

    with pytest.raises(main.HTTPException) as erro:
        main.extrair_zip_seguro(zip_path, alvo)
    assert erro.value.status_code == 400


def test_zip_slip_com_diretorio_irmao_de_prefixo(tmp_path):
    """O caso que a v2.3.0 deixava passar.

    O destino resolvido é "<tmp>/extracted_evil/evil.py", que passa no
    startswith("<tmp>/extracted") usado antes, mas está fora da pasta de extração.
    """
    alvo = tmp_path / "extracted"
    alvo.mkdir()
    zip_path = tmp_path / "irmao.zip"
    zip_path.write_bytes(zip_em_memoria({"../extracted_evil/evil.py": "print(1)"}))

    with pytest.raises(main.HTTPException) as erro:
        main.extrair_zip_seguro(zip_path, alvo)
    assert erro.value.status_code == 400
    assert not (tmp_path / "extracted_evil").exists()


def test_symlink_no_zip_e_ignorado(tmp_path):
    alvo = tmp_path / "extracted"
    alvo.mkdir()
    info = zipfile.ZipInfo("atalho.py")
    info.external_attr = (0o120777 << 16)  # S_IFLNK
    zip_path = tmp_path / "link.zip"
    zip_path.write_bytes(zip_em_memoria({}, extras=[(info, "/etc/passwd")]))

    avisos = main.extrair_zip_seguro(zip_path, alvo)
    assert not (alvo / "atalho.py").exists()
    assert any("symlink" in aviso for aviso in avisos)


def test_separador_windows_e_normalizado(tmp_path):
    alvo = tmp_path / "extracted"
    alvo.mkdir()
    zip_path = tmp_path / "windows.zip"
    zip_path.write_bytes(zip_em_memoria({"pasta\\arquivo.py": "print(1)"}))

    avisos = main.extrair_zip_seguro(zip_path, alvo)
    assert (alvo / "pasta" / "arquivo.py").is_file()
    assert any("Windows" in aviso for aviso in avisos)


def test_zip_acima_do_teto_de_membros(tmp_path):
    alvo = tmp_path / "extracted"
    alvo.mkdir()
    zip_path = tmp_path / "muitos.zip"
    zip_path.write_bytes(zip_em_memoria({f"a{i}.py": "x = 1" for i in range(5)}))

    with pytest.raises(main.HTTPException) as erro:
        main.extrair_zip_seguro(zip_path, alvo, max_membros=2)
    assert erro.value.status_code == 413


def test_zip_bomb_por_tamanho(tmp_path):
    alvo = tmp_path / "extracted"
    alvo.mkdir()
    zip_path = tmp_path / "bomba.zip"
    zip_path.write_bytes(zip_em_memoria({"grande.py": "x = 1\n" * 5000}))

    with pytest.raises(main.HTTPException) as erro:
        main.extrair_zip_seguro(zip_path, alvo, max_bytes=100)
    assert erro.value.status_code == 413


def test_zip_bomb_por_razao_de_compressao(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "RAZAO_COMPRESSAO_MAX", 10)
    alvo = tmp_path / "extracted"
    alvo.mkdir()
    zip_path = tmp_path / "razao.zip"
    zip_path.write_bytes(zip_em_memoria({"repetitivo.py": "a" * 100_000}))

    with pytest.raises(main.HTTPException) as erro:
        main.extrair_zip_seguro(zip_path, alvo)
    assert erro.value.status_code == 413


# ------------------------------------------------------------------------------
# ruff: "sem avisos" != "não rodou"
# ------------------------------------------------------------------------------
def test_ruff_ausente_nao_vira_sem_avisos(monkeypatch, tmp_path):
    monkeypatch.setattr(main.shutil, "which", lambda _: None)
    arquivo = tmp_path / "a.py"
    arquivo.write_text("x = 1\n")

    status, erros, resumo, detalhe = main.rodar_ruff([arquivo], tmp_path)
    assert status is main.StatusRuff.INDISPONIVEL
    assert erros == [] and resumo == {}
    assert "não encontrado" in (detalhe or "")


def test_ruff_sem_arquivos_py(tmp_path):
    status, erros, _, _ = main.rodar_ruff([], tmp_path)
    assert status is main.StatusRuff.OK and erros == []


@pytest.mark.skipif(shutil.which("ruff") is None, reason="ruff não instalado")
def test_ruff_detecta_import_nao_usado(tmp_path):
    arquivo = tmp_path / "ruim.py"
    arquivo.write_text("import os\n\n\nvalor = 1\n")

    status, erros, resumo, _ = main.rodar_ruff([arquivo], tmp_path)
    assert status is main.StatusRuff.OK
    assert any(erro.codigo == "F401" for erro in erros), resumo


# ------------------------------------------------------------------------------
# indicativo de IA
# ------------------------------------------------------------------------------
def test_indicativo_ia_acusa_apenas_com_indicio_forte():
    corpo = "\n".join(
        f"# comentário explicativo número {i}\nvariavel_com_nome_longo_{i} = {i}"
        for i in range(60)
    )
    arquivos = [main.ArquivoAnalisado("m.py", ".py", corpo)]
    analise = main.avaliar_indicativo_ia(arquivos)

    assert analise.nivel_suspeita == "SIM"
    assert all(analise.criterios.values())
    assert analise.metricas.nomes_avaliados >= 10
    assert analise.limiares["LIMIAR_COMENTARIOS"] == main.LIMIAR_COMENTARIOS


def test_indicativo_ia_nao_acusa_entrega_pequena():
    arquivos = [main.ArquivoAnalisado("m.py", ".py", "x = 1\ny = 2\n")]
    analise = main.avaliar_indicativo_ia(arquivos)

    assert analise.nivel_suspeita == ""
    assert analise.criterios[f"linhas_codigo>={main.MIN_LINHAS_CODIGO}"] is False


# ------------------------------------------------------------------------------
# endpoint
# ------------------------------------------------------------------------------
def test_check_feliz():
    zip_bytes = zip_em_memoria({"projeto/main.py": CODIGO_OK, "README.md": "# oi\n"})
    resposta = entregar(zip_bytes, aluno_id="12345", semestre="2026-1", atividade="lista1")

    assert resposta.status_code == 200, resposta.text
    dados = resposta.json()

    assert dados["versao_analisador"] == main.VERSAO_ANALISADOR
    assert dados["aluno_id"] == "12345"
    assert dados["atividade"] == "lista1"
    assert dados["sintaxe"]["valido"] is True
    assert dados["erros_sintaxe"] == []
    assert dados["hash_codigo"] != "n/a"
    assert "projeto/main.py" in dados["codigo_completo"]
    assert "ANÁLISE AUTOMÁTICA" in dados["contexto_llm"]
    assert dados["codigo_truncado"] is False
    assert dados["analise_ia"]["metricas"]["linhas_codigo"] > 0


def test_check_rejeita_arquivo_sem_extensao_zip():
    resposta = entregar(b"qualquer coisa", nome="entrega.txt")
    assert resposta.status_code == 400


def test_check_rejeita_zip_corrompido():
    resposta = entregar(b"nao sou um zip")
    assert resposta.status_code == 400
    assert "ZIP válido" in resposta.json()["detail"]


def test_check_rejeita_zip_com_zip_slip():
    resposta = entregar(zip_em_memoria({"../evil.py": "print(1)"}))
    assert resposta.status_code == 400


def test_check_respeita_teto_de_upload(monkeypatch):
    monkeypatch.setattr(main, "MAX_UPLOAD_BYTES", 10)
    resposta = entregar(zip_em_memoria({"a.py": "x = 1\n" * 100}))
    assert resposta.status_code == 413


def test_check_trunca_codigo_grande(monkeypatch):
    monkeypatch.setattr(main, "MAX_CHARS_CODIGO_COMPLETO", 200)
    zip_bytes = zip_em_memoria({"a.py": "x = 1\n" * 1000, "b.py": "y = 2\n" * 1000})
    dados = entregar(zip_bytes).json()

    assert dados["codigo_truncado"] is True
    assert "CÓDIGO TRUNCADO" in dados["codigo_completo"]
    assert any("limite de tamanho" in aviso for aviso in dados["avisos"])
    assert "truncado" in dados["contexto_llm"]


def test_check_lista_todos_os_erros_de_sintaxe():
    """Antes: só o primeiro erro, sem nome de arquivo."""
    zip_bytes = zip_em_memoria({
        "a.py": "def quebrado(:\n    pass\n",
        "b.py": "esta linha não é python\n",
    })
    dados = entregar(zip_bytes).json()

    assert dados["sintaxe"]["valido"] is False
    assert dados["sintaxe"]["arquivo"] in {"a.py", "b.py"}
    assert {erro["arquivo"] for erro in dados["erros_sintaxe"]} == {"a.py", "b.py"}


def test_contexto_llm_avisa_quando_ruff_nao_roda(monkeypatch):
    monkeypatch.setattr(main.shutil, "which", lambda _: None)
    dados = entregar(zip_em_memoria({"a.py": CODIGO_OK})).json()

    assert dados["ruff_status"] == "indisponivel"
    assert "NÃO EXECUTADO" in dados["contexto_llm"]
    assert "não foi avaliada por lint" in dados["analise_resultado"]


def test_exige_token_quando_configurado(monkeypatch):
    monkeypatch.setattr(main, "API_TOKEN", "segredo")
    zip_bytes = zip_em_memoria({"a.py": CODIGO_OK})

    assert entregar(zip_bytes).status_code == 401
    ok = cliente.post(
        "/check",
        files={"file": ("entrega.zip", zip_bytes, "application/zip")},
        headers={"X-API-Key": "segredo"},
    )
    assert ok.status_code == 200


def test_healthz():
    dados = cliente.get("/healthz").json()
    assert dados["status"] == "ok"
    assert dados["versao"] == main.VERSAO_ANALISADOR
