"""Code Evaluator API — análise estática e triagem de autoria de entregas.

Contrato HTTP consumido pelo workflow n8n (nó "Analisar Código (Ruff)"):

    POST /check   multipart/form-data
        file       (obrigatório)  ZIP da entrega
        aluno_id   (opcional)     RA / matrícula — ecoado no relatório
        semestre   (opcional)     semestre/disciplina — ecoado no relatório
        atividade  (opcional)     identificador da atividade — ecoado no relatório
    GET  /healthz

Campos novos em relação à v2.3.0 (os antigos continuam no mesmo lugar):
    versao_analisador, versao_regras, hash_entrega, erros_sintaxe, ruff_status,
    ruff_detalhe, ruff_resumo, codigo_truncado, avisos, contexto_llm, metricas
    dentro de analise_ia, e os campos de eco (aluno_id/semestre/atividade).

Compatibilidade deliberada:
  * ``hash_codigo`` mantém EXATAMENTE o algoritmo antigo (mesmo conteúdo → mesmo
    valor), para não invalidar o histórico da planilha. A única diferença é que
    agora os arquivos entram em ordem canônica (antes a ordem vinha do os.walk,
    ou seja, o hash podia mudar entre execuções). Se o histórico tiver valores
    gravados de entregas cuja ordem de arquivos não era alfabética, os hashes
    futuros dessas mesmas entregas podem divergir dos antigos.
  * ``analise_ia.nivel_suspeita`` continua devolvendo "SIM" ou "".

Variáveis de ambiente (todas opcionais, com padrão):
    MAX_UPLOAD_MB             25    teto do ZIP enviado
    MAX_DESCOMPACTADO_MB      300   teto do total descompactado (anti zip bomb)
    RAZAO_COMPRESSAO_MAX      500   teto da razão descompactado/comprimido
    MAX_MEMBROS_ZIP           1000  teto de entradas no ZIP
    MAX_ARQUIVOS_ANALISADOS   400   teto de arquivos lidos para análise
    MAX_CHARS_CODIGO_COMPLETO 60000 teto do texto enviado ao LLM
    RUFF_TIMEOUT_SEGUNDOS     60    timeout do subprocesso do ruff
    MAX_ERROS_SINTAXE         50    teto de erros de sintaxe listados
    RUFF_CONFIG                     caminho do ruff.toml (padrão: ao lado deste arquivo)
    API_TOKEN                       se definido, exige header X-API-Key
    LIMIAR_COMENTARIOS        0.25
    LIMIAR_MEDIA_NOMES        10.0
    LIMIAR_NOMES_CURTOS       0.05
    MIN_LINHAS_CODIGO         60
    MIN_NOMES_AVALIAVEIS      10
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import logging
import os
import shutil
import stat
import subprocess
import tempfile
import tokenize
import zipfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from pydantic import BaseModel

# ------------------------------------------------------------------------------
# CONFIGURAÇÃO
# ------------------------------------------------------------------------------
VERSAO_ANALISADOR = "3.0.0"

DIRETORIO_SCRIPT = Path(__file__).resolve().parent


def _env_int(nome: str, padrao: int) -> int:
    try:
        return int(os.environ[nome])
    except (KeyError, TypeError, ValueError):
        return padrao


def _env_float(nome: str, padrao: float) -> float:
    try:
        return float(os.environ[nome])
    except (KeyError, TypeError, ValueError):
        return padrao


MAX_UPLOAD_BYTES = _env_int("MAX_UPLOAD_MB", 25) * 1024 * 1024
MAX_DESCOMPACTADO_BYTES = _env_int("MAX_DESCOMPACTADO_MB", 300) * 1024 * 1024
RAZAO_COMPRESSAO_MAX = _env_int("RAZAO_COMPRESSAO_MAX", 500)
MAX_MEMBROS_ZIP = _env_int("MAX_MEMBROS_ZIP", 1000)
MAX_ARQUIVOS_ANALISADOS = _env_int("MAX_ARQUIVOS_ANALISADOS", 400)
MAX_CHARS_CODIGO_COMPLETO = _env_int("MAX_CHARS_CODIGO_COMPLETO", 60_000)
RUFF_TIMEOUT_SEGUNDOS = _env_int("RUFF_TIMEOUT_SEGUNDOS", 60)
MAX_ERROS_SINTAXE = _env_int("MAX_ERROS_SINTAXE", 50)
MAX_EXEMPLOS_RUFF = _env_int("MAX_EXEMPLOS_RUFF", 15)
RUFF_CONFIG = os.environ.get("RUFF_CONFIG") or str(DIRETORIO_SCRIPT / "ruff.toml")
API_TOKEN = os.environ.get("API_TOKEN") or ""

EXTENSOES_PERMITIDAS = {".py", ".html", ".js", ".css", ".md", ".json"}
# Só estes entram na triagem de autoria: .md e .json não são código e distorceriam
# a densidade de comentários.
EXTENSOES_CODIGO = {".py", ".js", ".html", ".css"}
DIRETORIOS_IGNORADOS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".idea", ".vscode",
    ".pytest_cache", ".ruff_cache", "dist", "build", ".mypy_cache",
}
ARQUIVOS_DEPENDENCIA = {
    "requirements.txt", "pyproject.toml", "environment.yml", "Pipfile",
    "poetry.lock", "Pipfile.lock", "setup.py", "setup.cfg",
}

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("code-evaluator")

app = FastAPI(title="Code Evaluator API", version=VERSAO_ANALISADOR)


# ------------------------------------------------------------------------------
# SCHEMAS PYDANTIC
# ------------------------------------------------------------------------------
class DiagnosticoSintaxe(BaseModel):
    valido: bool
    erro_mensagem: Optional[str] = None
    linha_erro: Optional[int] = None
    arquivo: Optional[str] = None


class ErroSintaxe(BaseModel):
    arquivo: str
    linha: Optional[int] = None
    mensagem: str


class OcorrenciaRuff(BaseModel):
    arquivo: str
    linha: Optional[int] = None
    coluna: Optional[int] = None
    codigo: str
    mensagem: str


class MetricasAutoria(BaseModel):
    """Métricas brutas que sustentam (ou não) o indicativo de IA.

    Vão na resposta de propósito: sem elas o rótulo gravado na planilha não é
    auditável nem calibrável, e o aluno não tem como contestar.
    """

    linhas_total: int
    linhas_codigo: int
    comentarios: int
    densidade_comentarios: float
    nomes_avaliados: int
    nomes_unicos: int
    media_comprimento_nomes: float
    proporcao_nomes_curtos: float
    arquivos_considerados: int
    arquivos_com_nomes: int


class AnaliseIA(BaseModel):
    """Indicativo de IA: "SIM" ou "" (vazio = não acusa)."""

    nivel_suspeita: str
    criterios: Dict[str, bool]
    metricas: MetricasAutoria
    limiares: Dict[str, float]


class StatusRuff(str, Enum):
    OK = "ok"
    INDISPONIVEL = "indisponivel"
    TIMEOUT = "timeout"
    ERRO = "erro"


@dataclass
class ArquivoAnalisado:
    """Arquivo de código já lido do ZIP, pronto para a extração de métricas."""

    caminho: str
    extensao: str
    conteudo: str


class ResultadoAnalise(BaseModel):
    versao_analisador: str
    versao_regras: str
    hash_codigo: str
    hash_entrega: str
    total_arquivos: int
    arquivos_analisados: int
    possui_dependencias: bool
    sintaxe: DiagnosticoSintaxe
    erros_sintaxe: List[ErroSintaxe]
    ruff_status: StatusRuff
    ruff_detalhe: Optional[str] = None
    erros_ruff: List[OcorrenciaRuff]
    ruff_resumo: Dict[str, int]
    analise_ia: AnaliseIA
    codigo_completo: str
    codigo_truncado: bool
    avisos: List[str]
    aluno_id: Optional[str] = None
    semestre: Optional[str] = None
    atividade: Optional[str] = None
    contexto_llm: str
    analise_resultado: Optional[str] = None


# ------------------------------------------------------------------------------
# INDICATIVO DE IA x HUMANO (na dúvida, não acusa)
# ------------------------------------------------------------------------------
# Marca "SIM" só quando comentários E nomes são ambos fortes; qualquer dúvida
# deixa o campo VAZIO. Vazio = "não acusa" — nunca significa "código humano
# verificado". É indício para priorizar revisão, não prova, e não deve alimentar
# nota. Se o campo ficar sempre vazio nas entregas reais, o primeiro botão a
# girar é LIMIAR_COMENTARIOS (agora por env, sem redeploy).
LIMIAR_COMENTARIOS = _env_float("LIMIAR_COMENTARIOS", 0.25)
LIMIAR_MEDIA_NOMES = _env_float("LIMIAR_MEDIA_NOMES", 10.0)
LIMIAR_NOMES_CURTOS = _env_float("LIMIAR_NOMES_CURTOS", 0.05)
MIN_LINHAS_CODIGO = _env_int("MIN_LINHAS_CODIGO", 60)
MIN_NOMES_AVALIAVEIS = _env_int("MIN_NOMES_AVALIAVEIS", 10)
ROTULO_INDICIO = "SIM"
NOMES_IGNORADOS = {"self", "cls"}


@dataclass
class _Medidas:
    """Acumulador de linhas, comentários e nomes, somável entre arquivos."""

    linhas: int = 0
    comentarios: int = 0
    nomes: int = 0
    soma_comprimento: int = 0
    curtos: int = 0
    arquivos: int = 0
    arquivos_com_nomes: int = 0
    nomes_vistos: set = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.nomes_vistos is None:
            self.nomes_vistos = set()

    def somar(self, outra: "_Medidas") -> None:
        self.linhas += outra.linhas
        self.comentarios += outra.comentarios
        self.nomes += outra.nomes
        self.soma_comprimento += outra.soma_comprimento
        self.curtos += outra.curtos
        self.arquivos += outra.arquivos
        self.arquivos_com_nomes += outra.arquivos_com_nomes
        self.nomes_vistos |= outra.nomes_vistos


# Configuração de comentários por linguagem: (marcador de linha, ini bloco, fim bloco).
# O CSS deliberadamente NÃO tem marcador de linha: "#fff" (cor) e "--minha-var"
# (custom property) não são comentários e inflavam a densidade.
_COMENTARIOS_POR_EXTENSAO: Dict[str, Tuple[Optional[str], Optional[str], Optional[str]]] = {
    ".js": ("//", "/*", "*/"),
    ".css": (None, "/*", "*/"),
    ".html": (None, "<!--", "-->"),
}


def _contar_comentarios(
    texto: str,
    marcador_linha: Optional[str] = None,
    inicio_bloco: Optional[str] = None,
    fim_bloco: Optional[str] = None,
) -> int:
    """Conta LINHAS com comentário via varredura de caracteres com estado.

    Strings são puladas (com escape) para que `"http://x"` não vire comentário em
    JS, e o estado de bloco é mantido entre linhas, ao contrário do antigo
    `startswith` por linha, que errava o miolo e o fim de comentários multi-linha.
    """
    linhas_comentario: set = set()
    i = 0
    linha = 1
    total = len(texto)
    em_bloco = False
    aspas: Optional[str] = None

    while i < total:
        caractere = texto[i]

        if caractere == "\n":
            linha += 1
            i += 1
            continue

        if em_bloco:
            linhas_comentario.add(linha)
            if fim_bloco and texto.startswith(fim_bloco, i):
                em_bloco = False
                i += len(fim_bloco)
            else:
                i += 1
            continue

        if aspas is not None:
            if caractere == "\\":
                i += 2
                continue
            if caractere == aspas:
                aspas = None
            i += 1
            continue

        if caractere in ("\"", "'", "`"):
            aspas = caractere
            i += 1
            continue

        if inicio_bloco and texto.startswith(inicio_bloco, i):
            em_bloco = True
            linhas_comentario.add(linha)
            i += len(inicio_bloco)
            continue

        if marcador_linha and texto.startswith(marcador_linha, i):
            linhas_comentario.add(linha)
            fim = texto.find("\n", i)
            if fim == -1:
                break
            i = fim
            continue

        i += 1

    return len(linhas_comentario)


def _medir_generico(conteudo: str, extensao: str = "") -> _Medidas:
    """Linhas e comentários de arquivos não-Python (JS/CSS/HTML).

    Nomes exigem AST, então aqui não se mede nome nenhum: a média de nomes da
    entrega inteira sai só dos arquivos .py.
    """
    linhas = [linha for linha in conteudo.splitlines() if linha.strip()]
    marcador, ini, fim = _COMENTARIOS_POR_EXTENSAO.get(
        extensao, (None, None, None)
    )
    comentarios = _contar_comentarios(conteudo, marcador, ini, fim)
    return _Medidas(linhas=len(linhas), comentarios=comentarios, arquivos=1)


def _medir_python(conteudo: str) -> _Medidas:
    """Conta linhas, comentários/docstrings e nomes de variáveis/funções de um .py.

    Comentários via `tokenize` (nunca contando delimitadores de docstring por
    texto, o que corromperia o estado do parser); docstrings via AST.

    Nomes considerados: apenas o que o aluno escolheu — atribuições, variáveis de
    laço, parâmetros e nomes de função/classe. Nomes em contexto de leitura (API
    da linguagem, como `range` e `print`) e imports ficam de fora, senão o
    vocabulário da stdlib distorceria a média.

    Docstrings contadas: só as de função/classe (linhas que ocupam). Docstring de
    módulo fica de fora — herdado da v2.3.0 e mantido de propósito, porque
    subestimar a densidade erra para o lado de marcar menos gente injustamente.
    """
    linhas = conteudo.splitlines()
    nao_vazias = [linha for linha in linhas if linha.strip()]

    linhas_comentario: set = set()
    try:
        for token in tokenize.generate_tokens(io.StringIO(conteudo).readline):
            if token.type == tokenize.COMMENT:
                linhas_comentario.add(token.start[0])
    except (tokenize.TokenError, IndentationError, SyntaxError, ValueError):
        linhas_comentario = {
            numero for numero, linha in enumerate(linhas, start=1)
            if linha.lstrip().startswith("#")
        }

    medidas = _Medidas(
        linhas=len(nao_vazias),
        comentarios=len(linhas_comentario),
        arquivos=1,
    )

    try:
        arvore = ast.parse(conteudo)
    except (SyntaxError, ValueError):
        return medidas

    nomes: List[str] = []
    docstrings = 0
    for node in ast.walk(arvore):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            nomes.append(node.name)
            primeiro = node.body[0] if node.body else None
            if (isinstance(primeiro, ast.Expr)
                    and isinstance(primeiro.value, ast.Constant)
                    and isinstance(primeiro.value.value, str)):
                fim = primeiro.end_lineno or primeiro.lineno
                docstrings += max(1, fim - primeiro.lineno + 1)
        elif isinstance(node, ast.arg):
            nomes.append(node.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            nomes.append(node.id)

    nomes = [nome for nome in nomes if nome not in NOMES_IGNORADOS]
    medidas.comentarios += docstrings
    medidas.nomes = len(nomes)
    medidas.soma_comprimento = sum(len(nome) for nome in nomes)
    medidas.curtos = sum(1 for nome in nomes if len(nome) <= 2)
    if nomes:
        medidas.arquivos_com_nomes = 1
        medidas.nomes_vistos = set(nomes)
    return medidas


def avaliar_indicativo_ia(arquivos: List[ArquivoAnalisado]) -> AnaliseIA:
    """Devolve nivel_suspeita = "SIM" só com indício forte; senão, vazio.

    Critérios, que precisam valer ao mesmo tempo:
      1. comentários/docstrings >= LIMIAR_COMENTARIOS das linhas não vazias;
      2. nomes com média >= LIMIAR_MEDIA_NOMES caracteres e <= LIMIAR_NOMES_CURTOS
         de nomes curtos.

    Também exige amostra mínima (MIN_LINHAS_CODIGO e MIN_NOMES_AVALIAVEIS).
    Toda dúvida leva a vazio: o campo é um indício, não uma acusação.

    A conta é idêntica à da v2.3.0 (mesmos limiares, mesma média sobre nomes
    repetidos) para não mudar em silêncio quem é marcado; o que muda é que agora
    as métricas e os critérios saem na resposta.
    """
    medidas = _Medidas()
    for arquivo in arquivos:
        medidas.somar(
            _medir_python(arquivo.conteudo)
            if arquivo.extensao == ".py"
            else _medir_generico(arquivo.conteudo, arquivo.extensao)
        )

    linhas_codigo = max(0, medidas.linhas - medidas.comentarios)
    densidade = medidas.comentarios / medidas.linhas if medidas.linhas else 0.0
    media_nomes = medidas.soma_comprimento / medidas.nomes if medidas.nomes else 0.0
    proporcao_curtos = medidas.curtos / medidas.nomes if medidas.nomes else 0.0

    criterios = {
        f"linhas_codigo>={MIN_LINHAS_CODIGO}": linhas_codigo >= MIN_LINHAS_CODIGO,
        f"densidade_comentarios>={LIMIAR_COMENTARIOS}": densidade >= LIMIAR_COMENTARIOS,
        f"nomes_avaliados>={MIN_NOMES_AVALIAVEIS}": medidas.nomes >= MIN_NOMES_AVALIAVEIS,
        f"media_comprimento_nomes>={LIMIAR_MEDIA_NOMES}": media_nomes >= LIMIAR_MEDIA_NOMES,
        f"proporcao_nomes_curtos<={LIMIAR_NOMES_CURTOS}": proporcao_curtos <= LIMIAR_NOMES_CURTOS,
    }
    indicio = all(criterios.values())

    metricas = MetricasAutoria(
        linhas_total=medidas.linhas,
        linhas_codigo=linhas_codigo,
        comentarios=medidas.comentarios,
        densidade_comentarios=round(densidade, 4),
        nomes_avaliados=medidas.nomes,
        nomes_unicos=len(medidas.nomes_vistos),
        media_comprimento_nomes=round(media_nomes, 2),
        proporcao_nomes_curtos=round(proporcao_curtos, 4),
        arquivos_considerados=medidas.arquivos,
        arquivos_com_nomes=medidas.arquivos_com_nomes,
    )
    limiares = {
        "LIMIAR_COMENTARIOS": LIMIAR_COMENTARIOS,
        "LIMIAR_MEDIA_NOMES": LIMIAR_MEDIA_NOMES,
        "LIMIAR_NOMES_CURTOS": LIMIAR_NOMES_CURTOS,
        "MIN_LINHAS_CODIGO": float(MIN_LINHAS_CODIGO),
        "MIN_NOMES_AVALIAVEIS": float(MIN_NOMES_AVALIAVEIS),
    }
    return AnaliseIA(
        nivel_suspeita=ROTULO_INDICIO if indicio else "",
        criterios=criterios,
        metricas=metricas,
        limiares=limiares,
    )


# ------------------------------------------------------------------------------
# SEGURANÇA DO ZIP
# ------------------------------------------------------------------------------
def _extrair_membro(
    arquivo_zip: zipfile.ZipFile,
    membro: zipfile.ZipInfo,
    destino: Path,
    limite_restante: int,
) -> int:
    """Extrai um membro contando os bytes realmente escritos (não os declarados)."""
    escritos = 0
    destino.parent.mkdir(parents=True, exist_ok=True)
    with arquivo_zip.open(membro) as origem, open(destino, "wb") as saida:
        while True:
            bloco = origem.read(1 << 20)
            if not bloco:
                break
            escritos += len(bloco)
            if escritos > limite_restante:
                raise HTTPException(
                    status_code=413,
                    detail="Conteúdo descompactado acima do limite permitido "
                           "(possível zip bomb).",
                )
            saida.write(bloco)
    return escritos


def extrair_zip_seguro(
    zip_file_path: Path,
    extract_to: Path,
    max_bytes: int = MAX_DESCOMPACTADO_BYTES,
    max_membros: int = MAX_MEMBROS_ZIP,
) -> List[str]:
    """Extrai o ZIP garantindo que nada seja escrito fora de `extract_to`.

    Checagens: caminho resolvido contido na raiz (Zip Slip), rejeição de
    symlinks, teto de membros, teto de bytes escritos e razão de compressão.
    Devolve a lista de avisos (nomes normalizados, membros ignorados).
    """
    avisos: List[str] = []
    raiz = extract_to.resolve()
    total_escrito = 0

    with zipfile.ZipFile(zip_file_path, "r") as arquivo_zip:
        membros = arquivo_zip.infolist()
        if len(membros) > max_membros:
            raise HTTPException(
                status_code=413,
                detail=f"ZIP com {len(membros)} entradas excede o limite de {max_membros}.",
            )

        for membro in membros:
            nome_original = membro.filename
            if not nome_original or membro.is_dir():
                continue

            # ZIPs criados no Windows às vezes usam "\" como separador; sem
            # normalizar, "..\..\evil.py" viraria um nome de arquivo literal e
            # escaparia da checagem de caminho.
            nome = nome_original.replace("\\", "/")
            if nome != nome_original:
                avisos.append(f"separador Windows normalizado: {nome_original}")

            destino = (raiz / nome).resolve()
            if destino != raiz and raiz not in destino.parents:
                raise HTTPException(
                    status_code=400,
                    detail="Arquivo ZIP inválido ou malicioso (tentativa de Zip Slip).",
                )

            modo = membro.external_attr >> 16
            if modo and stat.S_ISLNK(modo):
                avisos.append(f"symlink ignorado: {nome}")
                continue

            if (membro.compress_size
                    and membro.file_size / max(membro.compress_size, 1) > RAZAO_COMPRESSAO_MAX):
                raise HTTPException(
                    status_code=413,
                    detail=f"Razão de compressão suspeita em {nome} (possível zip bomb).",
                )

            total_escrito += _extrair_membro(
                arquivo_zip, membro, destino, max_bytes - total_escrito
            )

    return avisos


# ------------------------------------------------------------------------------
# SINTAXE, HASH E RUFF
# ------------------------------------------------------------------------------
def validar_sintaxe_python(conteudo: str) -> DiagnosticoSintaxe:
    try:
        ast.parse(conteudo)
        return DiagnosticoSintaxe(valido=True)
    except SyntaxError as e:
        return DiagnosticoSintaxe(
            valido=False,
            erro_mensagem=e.msg,
            linha_erro=e.lineno,
        )


def coletar_erros_sintaxe(
    arquivos: List[ArquivoAnalisado],
) -> List[ErroSintaxe]:
    """Todos os erros de sintaxe, de todos os arquivos .py (não só o primeiro)."""
    erros: List[ErroSintaxe] = []
    for arquivo in arquivos:
        if arquivo.extensao != ".py":
            continue
        diagnostico = validar_sintaxe_python(arquivo.conteudo)
        if diagnostico.valido:
            continue
        erros.append(
            ErroSintaxe(
                arquivo=arquivo.caminho,
                linha=diagnostico.linha_erro,
                mensagem=diagnostico.erro_mensagem or "erro de sintaxe",
            )
        )
        if len(erros) >= MAX_ERROS_SINTAXE:
            break
    return erros


def calcular_hash_normalizado(codigos_python: List[str]) -> str:
    """Hash do conteúdo .py sem espaços em branco (mesmo algoritmo da v2.3.0)."""
    linhas_normalizadas = []
    for codigo in codigos_python:
        for linha in codigo.splitlines():
            linha_strip = linha.strip()
            if linha_strip:
                linhas_normalizadas.append(linha_strip)
    texto_normalizado = "\n".join(linhas_normalizadas)
    return hashlib.sha256(texto_normalizado.encode("utf-8")).hexdigest()[:16]


def calcular_hash_entrega(arquivos: List[ArquivoAnalisado]) -> str:
    """Hash da entrega inteira (caminho + conteúdo, ordem canônica)."""
    digest = hashlib.sha256()
    for arquivo in sorted(arquivos, key=lambda a: a.caminho):
        digest.update(arquivo.caminho.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(arquivo.conteudo.encode("utf-8", errors="replace"))
        digest.update(b"\x00")
    return digest.hexdigest()[:16]


def rodar_ruff(
    caminhos_py: List[Path],
    raiz: Path,
) -> Tuple[StatusRuff, List[OcorrenciaRuff], Dict[str, int], Optional[str]]:
    """Roda o ruff e distingue "sem avisos" de "o ruff não rodou".

    O retorno antigo (`except: return []`) fazia o relatório afirmar que não havia
    problema nenhum quando o binário estava ausente ou travado — ou seja, dava
    nota melhor por falha de infraestrutura.

    Recebe a lista de arquivos .py que a própria API selecionou (não a pasta
    inteira), para que a régua do lint e o escopo da análise sejam os mesmos.
    """
    if not caminhos_py:
        return StatusRuff.OK, [], {}, None

    if shutil.which("ruff") is None:
        log.warning("ruff não encontrado no PATH; análise estática não executada")
        return StatusRuff.INDISPONIVEL, [], {}, "executável 'ruff' não encontrado no PATH"

    comando = [
        "ruff", "check", *[str(c) for c in caminhos_py],
        "--output-format=json", "--no-cache", "--quiet", "--force-exclude",
    ]

    if RUFF_CONFIG and Path(RUFF_CONFIG).is_file():
        comando += ["--config", RUFF_CONFIG]

    try:
        resultado = subprocess.run(
            comando,
            capture_output=True,
            text=True,
            check=False,
            timeout=RUFF_TIMEOUT_SEGUNDOS,
            cwd=str(raiz),
        )
    except subprocess.TimeoutExpired:
        log.warning("ruff excedeu %ss", RUFF_TIMEOUT_SEGUNDOS)
        return (
            StatusRuff.TIMEOUT,
            [],
            {},
            f"ruff excedeu o limite de {RUFF_TIMEOUT_SEGUNDOS}s",
        )
    except OSError as e:
        log.warning("falha ao executar o ruff: %s", e)
        return StatusRuff.INDISPONIVEL, [], {}, f"falha ao executar o ruff: {e}"

    # Códigos do ruff: 0 = sem violações, 1 = violações, 2 = erro de execução.
    if resultado.returncode not in (0, 1):
        detalhe = (resultado.stderr or "").strip()[:500] or f"ruff retornou código {resultado.returncode}"
        log.warning("ruff falhou: %s", detalhe)
        return StatusRuff.ERRO, [], {}, detalhe

    try:
        bruto = json.loads(resultado.stdout or "[]")
    except json.JSONDecodeError as e:
        log.warning("saída do ruff não é JSON: %s", e)
        return StatusRuff.ERRO, [], {}, f"saída do ruff ilegível: {e}"

    ocorrencias: List[OcorrenciaRuff] = []
    resumo: Dict[str, int] = {}
    for item in bruto:
        local = item.get("filename") or ""
        try:
            local = str(Path(local).resolve().relative_to(raiz.resolve()))
        except ValueError:
            pass  # arquivo fora da pasta analisada: mantém o caminho cru
        codigo = (item.get("code") or "?").strip()
        resumo[codigo] = resumo.get(codigo, 0) + 1
        ocorrencias.append(
            OcorrenciaRuff(
                arquivo=local,
                linha=(item.get("location") or {}).get("row"),
                coluna=(item.get("location") or {}).get("column"),
                codigo=codigo,
                mensagem=(item.get("message") or "").strip(),
            )
        )

    ocorrencias.sort(key=lambda o: (o.arquivo, o.linha or 0, o.coluna or 0))
    return StatusRuff.OK, ocorrencias, resumo, None


def calcular_versao_regras() -> str:
    """Impressão digital da régua (ruff.toml + limiares), para auditoria da nota."""
    partes = [
        f"analisador={VERSAO_ANALISADOR}",
        f"LIMIAR_COMENTARIOS={LIMIAR_COMENTARIOS}",
        f"LIMIAR_MEDIA_NOMES={LIMIAR_MEDIA_NOMES}",
        f"LIMIAR_NOMES_CURTOS={LIMIAR_NOMES_CURTOS}",
        f"MIN_LINHAS_CODIGO={MIN_LINHAS_CODIGO}",
        f"MIN_NOMES_AVALIAVEIS={MIN_NOMES_AVALIAVEIS}",
    ]
    caminho_config = Path(RUFF_CONFIG) if RUFF_CONFIG else None
    if caminho_config and caminho_config.is_file():
        partes.append(f"ruff.toml={hashlib.sha256(caminho_config.read_bytes()).hexdigest()[:16]}")
    else:
        partes.append("ruff.toml=ausente(padrão do ruff)")
    return hashlib.sha256("|".join(partes).encode("utf-8")).hexdigest()[:12]


# ------------------------------------------------------------------------------
# RELATÓRIO
# ------------------------------------------------------------------------------
def _linhas_ruff_para_prompt(erros: List[OcorrenciaRuff]) -> List[str]:
    exemplos = []
    for erro in erros[:MAX_EXEMPLOS_RUFF]:
        local = f"{erro.arquivo}:{erro.linha}" if erro.linha else erro.arquivo
        exemplos.append(f"  - {local} [{erro.codigo}] {erro.mensagem}")
    if len(erros) > MAX_EXEMPLOS_RUFF:
        exemplos.append(f"  - ... e mais {len(erros) - MAX_EXEMPLOS_RUFF} ocorrência(s)")
    return exemplos


def montar_contexto_llm(
    *,
    resultado_ruff: Tuple[StatusRuff, List[OcorrenciaRuff], Dict[str, int], Optional[str]],
    erros_sintaxe: List[ErroSintaxe],
    analise_ia: AnaliseIA,
    total_arquivos: int,
    arquivos_analisados: int,
    codigo_truncado: bool,
    avisos: List[str],
) -> str:
    """Bloco de contexto para o prompt do avaliador.

    Existe porque o prompt da v1 pedia ao modelo para "considerar os avisos do
    relatório do Ruff acima" — e só recebia `codigo_completo`. Sem este bloco o
    modelo era obrigado a inventar (ou ignorar) o critério de lint.
    """
    ruff_status, erros_ruff, resumo_ruff, detalhe = resultado_ruff
    linhas: List[str] = []
    linhas.append(f"=== ANÁLISE AUTOMÁTICA (analisador v{VERSAO_ANALISADOR}) ===")

    if erros_sintaxe:
        linhas.append(f"Sintaxe Python: {len(erros_sintaxe)} erro(s) — o código NÃO compila:")
        for erro in erros_sintaxe[:MAX_ERROS_SINTAXE]:
            linhas.append(f"  - {erro.arquivo}:{erro.linha or '?'} → {erro.mensagem}")
    else:
        linhas.append("Sintaxe Python: válida em todos os arquivos .py analisados.")

    if ruff_status is StatusRuff.OK:
        total = len(erros_ruff)
        if total:
            por_regra = ", ".join(f"{k}={v}" for k, v in sorted(resumo_ruff.items()))
            linhas.append(f"Ruff: {total} ocorrência(s) — {por_regra}")
            linhas.extend(_linhas_ruff_para_prompt(erros_ruff))
        else:
            linhas.append("Ruff: nenhuma violação na régua configurada.")
    else:
        linhas.append(
            f"Ruff: NÃO EXECUTADO (status={ruff_status.value}"
            + (f", detalhe: {detalhe}" if detalhe else "")
            + "). Não presuma ausência de problemas de lint nem cite regras "
              "do Ruff no feedback: não há dado para isso nesta entrega."
        )

    metricas = analise_ia.metricas
    linhas.append(
        "Triagem de autoria (indício, NÃO use para justificar nota): "
        f"densidade_comentarios={metricas.densidade_comentarios}, "
        f"media_comprimento_nomes={metricas.media_comprimento_nomes}, "
        f"nomes_avaliados={metricas.nomes_avaliados}."
    )

    if codigo_truncado:
        linhas.append(
            f"ATENÇÃO: o código abaixo foi truncado em {MAX_CHARS_CODIGO_COMPLETO} caracteres. "
            "Partes da entrega podem não estar visíveis — não penalize por conteúdo ausente."
        )
    if arquivos_analisados < total_arquivos:
        linhas.append(
            f"ATENÇÃO: {arquivos_analisados} de {total_arquivos} arquivo(s) entraram na análise "
            f"(limite de {MAX_ARQUIVOS_ANALISADOS})."
        )
    for aviso in avisos[:10]:
        linhas.append(f"Aviso de empacotamento: {aviso}")

    return "\n".join(linhas)


def executar_analise_codigo(
    sintaxe: DiagnosticoSintaxe,
    erros_ruff: List[OcorrenciaRuff],
    analise_ia: AnaliseIA,
    semestre: Optional[str] = None,
    aluno_id: Optional[str] = None,
    erros_sintaxe: Optional[List[ErroSintaxe]] = None,
    ruff_status: StatusRuff = StatusRuff.OK,
    ruff_detalhe: Optional[str] = None,
    codigo_truncado: bool = False,
    avisos: Optional[List[str]] = None,
) -> str:
    """Relatório legível por humano (vai na planilha / inspeção manual)."""
    relatorio = ["--- RELATÓRIO DE ANÁLISE ---"]
    relatorio.append(f"Analisador: v{VERSAO_ANALISADOR} | régua: {calcular_versao_regras()}")
    if aluno_id:
        relatorio.append(f"Aluno ID: {aluno_id}")
    if semestre:
        relatorio.append(f"Semestre: {semestre}")

    relatorio.append("\n[1. Diagnóstico de Sintaxe]")
    if sintaxe.valido:
        relatorio.append("✔ Sintaxe válida em todos os arquivos Python.")
    else:
        relatorio.append(
            f"✖ Erro de sintaxe em {sintaxe.arquivo or '(arquivo não identificado)'}, "
            f"linha {sintaxe.linha_erro}: {sintaxe.erro_mensagem}"
        )
    for erro in (erros_sintaxe or [])[1:]:
        relatorio.append(f"✖ {erro.arquivo}:{erro.linha} → {erro.mensagem}")

    relatorio.append("\n[2. Indicativo de IA (na dúvida, não acusa)]")
    relatorio.append(
        f"• Campo gravado na planilha (prob_ia): {analise_ia.nivel_suspeita or '(vazio)'}"
    )
    metricas = analise_ia.metricas
    relatorio.append(
        "• Métricas: "
        f"{metricas.linhas_codigo} linha(s) de código, {metricas.comentarios} de comentário "
        f"(densidade {metricas.densidade_comentarios}), "
        f"{metricas.nomes_avaliados} nome(s) avaliado(s) "
        f"(média {metricas.media_comprimento_nomes} chars, "
        f"{metricas.proporcao_nomes_curtos} de nomes curtos)"
    )
    for criterio, atendido in analise_ia.criterios.items():
        relatorio.append(f"    {'✔' if atendido else '✖'} {criterio}")
    relatorio.append(
        "• Vazio = 'não acusa' — não significa que o código foi verificado como humano. "
        "Indício para priorizar revisão; não use para justificar nota."
    )

    relatorio.append("\n[3. Análise Estática - Ruff]")
    if ruff_status is StatusRuff.OK:
        if erros_ruff:
            relatorio.append(f"⚠ Foram encontrados {len(erros_ruff)} alertas de linting.")
            relatorio.extend(_linhas_ruff_para_prompt(erros_ruff))
        else:
            relatorio.append("✔ Nenhum aviso de qualidade apontado pelo Ruff.")
    else:
        relatorio.append(
            f"⚠ O Ruff NÃO rodou nesta entrega (status={ruff_status.value}"
            + (f": {ruff_detalhe}" if ruff_detalhe else "")
            + "). A qualidade de código não foi avaliada por lint."
        )

    if codigo_truncado or avisos:
        relatorio.append("\n[4. Ressalvas de empacotamento]")
        if codigo_truncado:
            relatorio.append(
                f"⚠ Código enviado ao avaliador truncado em {MAX_CHARS_CODIGO_COMPLETO} caracteres."
            )
        for aviso in (avisos or [])[:10]:
            relatorio.append(f"• {aviso}")

    return "\n".join(relatorio)


# ------------------------------------------------------------------------------
# LEITURA DE ARQUIVOS
# ------------------------------------------------------------------------------
def _ler_texto(caminho: Path) -> str:
    """UTF-8 com fallback para cp1252 (ZIP gerado no Windows com acentos)."""
    for codificacao in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return caminho.read_text(encoding=codificacao)
        except UnicodeDecodeError:
            continue
        except OSError:
            return ""
    return caminho.read_text(encoding="utf-8", errors="ignore")


def _verificar_token(token_enviado: Optional[str]) -> None:
    if API_TOKEN and token_enviado != API_TOKEN:
        raise HTTPException(status_code=401, detail="X-API-Key ausente ou inválida.")


def _salvar_upload_limitado(arquivo: UploadFile, destino: Path) -> None:
    """Grava o upload em blocos, abortando ao passar do teto (evita OOM)."""
    total = 0
    with open(destino, "wb") as saida:
        while True:
            bloco = arquivo.file.read(1 << 20)
            if not bloco:
                break
            total += len(bloco)
            if total > MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"ZIP acima do limite de {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
                )
            saida.write(bloco)


# ------------------------------------------------------------------------------
# ENDPOINTS
# ------------------------------------------------------------------------------
@app.get("/healthz")
def healthz() -> dict:
    return {
        "status": "ok",
        "versao": VERSAO_ANALISADOR,
        "versao_regras": calcular_versao_regras(),
        "ruff_disponivel": shutil.which("ruff") is not None,
        "ruff_config": RUFF_CONFIG if Path(RUFF_CONFIG).is_file() else None,
    }


@app.post("/check", response_model=ResultadoAnalise)
def check_code(
    file: UploadFile = File(...),
    semestre: Optional[str] = Form(None),
    aluno_id: Optional[str] = Form(None),
    atividade: Optional[str] = Form(None),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    _verificar_token(x_api_key)

    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="O arquivo enviado deve ser no formato .zip")

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        zip_path = temp_path / "upload.zip"
        _salvar_upload_limitado(file, zip_path)

        if not zipfile.is_zipfile(zip_path):
            raise HTTPException(
                status_code=400,
                detail="O conteúdo enviado não é um ZIP válido (extensão .zip, arquivo corrompido?).",
            )

        pasta_extraida = temp_path / "extracted"
        pasta_extraida.mkdir()
        avisos = extrair_zip_seguro(zip_path, pasta_extraida)

        total_arquivos = 0
        possui_dependencias = False
        codigos_python: List[str] = []
        caminhos_py: List[Path] = []
        arquivos_analisados: List[ArquivoAnalisado] = []
        arquivos_omitidos: List[str] = []

        for root, dirs, files in os.walk(pasta_extraida):
            dirs[:] = sorted(d for d in dirs if d not in DIRETORIOS_IGNORADOS)

            for filename in sorted(files):
                filepath = Path(root) / filename
                extensao = filepath.suffix.lower()

                if filename.lower() in ARQUIVOS_DEPENDENCIA:
                    possui_dependencias = True

                if extensao not in EXTENSOES_PERMITIDAS:
                    continue

                total_arquivos += 1
                caminho_relativo = filepath.relative_to(pasta_extraida)

                if len(arquivos_analisados) >= MAX_ARQUIVOS_ANALISADOS:
                    arquivos_omitidos.append(str(caminho_relativo))
                    continue

                conteudo = _ler_texto(filepath)
                # Só código-fonte entra na triagem de autoria (.md/.json ficam fora).
                if extensao in EXTENSOES_CODIGO:
                    arquivos_analisados.append(
                        ArquivoAnalisado(
                            caminho=str(caminho_relativo),
                            extensao=extensao,
                            conteudo=conteudo,
                        )
                    )
                if extensao == ".py":
                    caminhos_py.append(filepath)

        if arquivos_omitidos:
            avisos.append(
                f"{len(arquivos_omitidos)} arquivo(s) acima do limite de "
                f"{MAX_ARQUIVOS_ANALISADOS} não foram analisados: "
                + ", ".join(arquivos_omitidos[:10])
            )

        # Ordem canônica: sem isso a ordem vinha do os.walk e o hash (e o texto
        # entregue ao LLM) mudava de execução para execução.
        arquivos_analisados.sort(key=lambda a: a.caminho)
        codigos_python = [
            a.conteudo for a in arquivos_analisados if a.extensao == ".py"
        ]

        # Monta o texto do código com teto explícito em vez de mandar tudo para
        # o LLM e deixar o provedor truncar (ou estourar a requisição) em silêncio.
        partes_codigo: List[str] = []
        usados = 0
        codigo_truncado = False
        for arquivo in arquivos_analisados:
            cabecalho = f"=== Arquivo: {arquivo.caminho} ===\n"
            restante = MAX_CHARS_CODIGO_COMPLETO - usados
            if restante <= len(cabecalho):
                codigo_truncado = True
                avisos.append(f"omitido por limite de tamanho: {arquivo.caminho}")
                continue
            corpo = arquivo.conteudo
            if len(cabecalho) + len(corpo) > restante:
                corpo = corpo[: max(0, restante - len(cabecalho))]
                codigo_truncado = True
                avisos.append(f"cortado por limite de tamanho: {arquivo.caminho}")
            partes_codigo.append(cabecalho + corpo + "\n")
            usados += len(cabecalho) + len(corpo) + 1
        if codigo_truncado:
            partes_codigo.append(
                "[... CÓDIGO TRUNCADO: partes da entrega foram omitidas deste texto. ...]\n"
            )
        codigo_completo = "\n".join(partes_codigo)

        erros_sintaxe = coletar_erros_sintaxe(arquivos_analisados)
        if erros_sintaxe:
            primeiro = erros_sintaxe[0]
            diagnostico_sintaxe = DiagnosticoSintaxe(
                valido=False,
                erro_mensagem=primeiro.mensagem,
                linha_erro=primeiro.linha,
                arquivo=primeiro.arquivo,
            )
        else:
            diagnostico_sintaxe = DiagnosticoSintaxe(valido=True)

        resultado_ruff = rodar_ruff(caminhos_py, pasta_extraida)
        ruff_status, erros_ruff, resumo_ruff, ruff_detalhe = resultado_ruff

        hash_codigo = calcular_hash_normalizado(codigos_python) if codigos_python else "n/a"
        hash_entrega = calcular_hash_entrega(arquivos_analisados) if arquivos_analisados else "n/a"

        # Indicativo de IA: só grava "SIM" com indício forte nos DOIS critérios.
        analise_ia = avaliar_indicativo_ia(arquivos_analisados)

        contexto_llm = montar_contexto_llm(
            resultado_ruff=resultado_ruff,
            erros_sintaxe=erros_sintaxe,
            analise_ia=analise_ia,
            total_arquivos=total_arquivos,
            arquivos_analisados=len(arquivos_analisados),
            codigo_truncado=codigo_truncado,
            avisos=avisos,
        )
        analise_resultado = executar_analise_codigo(
            sintaxe=diagnostico_sintaxe,
            erros_ruff=erros_ruff,
            analise_ia=analise_ia,
            semestre=semestre,
            aluno_id=aluno_id,
            erros_sintaxe=erros_sintaxe,
            ruff_status=ruff_status,
            ruff_detalhe=ruff_detalhe,
            codigo_truncado=codigo_truncado,
            avisos=avisos,
        )

        log.info(
            "check aluno=%s atividade=%s arquivos=%s ruff=%s hash=%s truncado=%s",
            aluno_id, atividade, len(arquivos_analisados), ruff_status.value,
            hash_entrega, codigo_truncado,
        )

        return ResultadoAnalise(
            versao_analisador=VERSAO_ANALISADOR,
            versao_regras=calcular_versao_regras(),
            hash_codigo=hash_codigo,
            hash_entrega=hash_entrega,
            total_arquivos=total_arquivos,
            arquivos_analisados=len(arquivos_analisados),
            possui_dependencias=possui_dependencias,
            sintaxe=diagnostico_sintaxe,
            erros_sintaxe=erros_sintaxe,
            ruff_status=ruff_status,
            ruff_detalhe=ruff_detalhe,
            erros_ruff=erros_ruff,
            ruff_resumo=resumo_ruff,
            analise_ia=analise_ia,
            codigo_completo=codigo_completo,
            codigo_truncado=codigo_truncado,
            avisos=avisos,
            aluno_id=aluno_id,
            semestre=semestre,
            atividade=atividade,
            contexto_llm=contexto_llm,
            analise_resultado=analise_resultado,
        )
