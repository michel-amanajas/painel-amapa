#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
atualizar_apuracao.py
======================
Busca os resultados oficiais do TSE para o Amapá (Governador, Senador,
Deputado Federal e Deputado Estadual) e grava um arquivo "dados.json"
que o painel local (painel_local.html) lê e exibe.

Roda 100% no seu computador (ou num servidor seu). Não usa Claude,
não consome tokens, não depende de internet além do site do TSE.

COMO USAR
---------
1) Não precisa instalar nada — usa só a biblioteca padrão do Python
   (urllib.request), que já vem em qualquer instalação do Python 3.

2) Rode uma vez, para testar:
     python atualizar_apuracao.py --once

3) Deixe rodando em loop no dia da eleição (ex.: a cada 5 minutos):
     python atualizar_apuracao.py --loop --intervalo 300

4) Em outro terminal, sirva a pasta para ver o painel no navegador:
     python -m http.server 8000
   e abra http://localhost:8000/painel_local.html
   (o painel recarrega os dados sozinho a cada 20s)

5) Para trazer a foto oficial de cadastro de cada candidato (a mesma
   usada no site do TSE), use --fotos:
     python atualizar_apuracao.py --once --fotos
   Na PRIMEIRA vez que rodar com --fotos, o script baixa do Portal de
   Dados Abertos do TSE (dadosabertos.tse.jus.br) a lista oficial de
   candidatos do Amapá (CSV) e o pacote de fotos do Amapá (ZIP), extrai
   só as fotos dos candidatos que aparecem no painel, salva cada uma em
   fotos/<sqcandidato>.jpg e grava um "mapa" em fotos_mapa.json (nome do
   candidato -> caminho da foto). Nas próximas vezes (inclusive no loop
   de 5 em 5 minutos), ele só relê esse mapa local — não baixa tudo de
   novo. Quem não tiver foto no pacote do TSE continua aparecendo com as
   iniciais coloridas no painel. Lembre de commitar a pasta fotos/ e o
   fotos_mapa.json junto com o dados.json (o workflow do GitHub Actions
   já faz isso automaticamente).

SOBRE O AMBIENTE DE PRODUÇÃO (confirmado em 28/09/2026)
---------------------------------------------------------
BASE_URL já aponta para o ambiente OFICIAL do TSE:
    https://resultados.tse.jus.br/oficial · pleito 3220 (1º turno, 04/10/2026)
    eleição 6259 = Estadual (Governador, Dep. Estadual)
    eleição 6257 = Federal (Senador, Dep. Federal)

Até o TSE ativar esse caminho (normalmente perto ou no dia da eleição),
as buscas vão dar 404 — isso é esperado, não é erro do script. Nesse
período, o script tenta automaticamente o ambiente de SIMULAÇÃO como
fallback, então dá para continuar testando com --once normalmente.

Se algo mudar (nomes de campo, códigos) quando o TSE ativar de verdade,
rode com --once e confira o log linha por linha antes de deixar em --loop.
Se nenhuma URL funcionar, o script avisa no terminal e NÃO sobrescreve o
dados.json com informação incompleta (ele preserva o último arquivo
válido).
"""

import argparse
import csv
import io
import json
import re
import sys
import time
import urllib.request
import urllib.error
import zipfile
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# CONFIGURAÇÃO — ajuste aqui antes do dia da eleição
# ---------------------------------------------------------------------------

# Domínio + ambiente PRODUÇÃO — código confirmado por Michel em 28/09/2026:
#   url: https://resultados.tse.jus.br · ambiente: oficial · pleito: 3220
#   eleição 6257 = Eleição Geral Federal (Senador, Deputado Federal)
#   eleição 6259 = Eleições Gerais Estaduais (Governador, Deputado Estadual)
# Até o TSE ativar esse caminho (normalmente perto/no dia da eleição), as
# buscas vão dar 404 — isso é esperado, não é erro do script. O script
# também tenta o ambiente de SIMULAÇÃO como fallback (ver urls_cargo),
# então nada muda no seu jeito de usar: --once continua funcionando, só
# passa a preferir os dados reais assim que eles ficarem disponíveis.
BASE_URL = "https://resultados.tse.jus.br/oficial"
BASE_URL_SIMULADO = "https://resultados-sim.tse.jus.br/simulado/simulado2026"

UF = "ap"                 # Amapá
PLEITO = "3220"           # código do pleito 1º turno 04/10/2026 (confirmado)

# Código de cargo em cada arquivo do TSE. Cada cargo tem sua própria
# "eleição" — governador/dep. estadual ficam na eleição Estadual (6259),
# senador/dep. federal ficam na eleição Federal (6257), mesmo sendo tudo
# o mesmo pleito e a mesma data de votação.
CARGOS = {
    "governador":   {"codigo": "3", "eleicao": "6259", "label": "Governador",         "proportional": False, "seats": 1},
    "senador":      {"codigo": "5", "eleicao": "6259", "label": "Senador",            "proportional": False, "seats": 2},
    "dep_federal":  {"codigo": "6", "eleicao": "6259", "label": "Deputado Federal",   "proportional": True,  "seats": 8},
    "dep_estadual": {"codigo": "7", "eleicao": "6259", "label": "Deputado Estadual",  "proportional": True,  "seats": 24},
}

# Códigos usados só como fallback no ambiente de simulação (validados
# anteriormente contra o simulado, podem não valer para o ambiente oficial).
PLEITO_SIMULADO = "17801"
ELEICAO_SIMULADO = "21272"

# Os 16 municípios do Amapá com seus códigos IBGE/TSE (para o arquivo de
# apuração por município). Ajuste/confira se o TSE usar códigos diferentes
# no ambiente de produção.
MUNICIPIOS = {
    "06050": "Macapá",
    "06084": "Santana",
    "06131": "Laranjal do Jari",
    "06166": "Oiapoque",
    "06033": "Mazagão",
    "06025": "Porto Grande",
    "06017": "Ferreira Gomes",
    "06009": "Amapá",
    "06041": "Pedra Branca do Amapari",
    "06068": "Pracuúba",
    "06076": "Tartarugalzinho",
    "06106": "Calçoene",
    "06114": "Cutias",
    "06122": "Itaubal",
    "06140": "Serra do Navio",
    "06157": "Vitória do Jari",
}

TIMEOUT_SEGUNDOS = 15
DADOS_JSON = Path(__file__).parent / "dados.json"


# ---------------------------------------------------------------------------
# Busca e parsing
# ---------------------------------------------------------------------------

def decodificar_corpo(bruto: bytes):
    """Decodifica o corpo baixado: JSON puro, ou JWS (3 partes separadas por
    '.', com o conteúdo na parte do meio em base64url), usado pelos arquivos
    .jws do TSE em 2026. Devolve o objeto Python ou levanta ValueError."""
    import base64, gzip, zlib
    texto = bruto.strip()
    if texto[:2] == b"\x1f\x8b":
        texto = gzip.decompress(texto)
    try:
        return json.loads(texto.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError):
        pass
    partes = texto.decode("ascii", "ignore").strip().strip('"').split(".")
    if len(partes) >= 2:
        carga = partes[1]
        carga += "=" * (-len(carga) % 4)
        dados = base64.urlsafe_b64decode(carga)
        for tentativa in (lambda d: d, gzip.decompress, zlib.decompress):
            try:
                return json.loads(tentativa(dados).decode("utf-8-sig"))
            except Exception:
                continue
    raise ValueError("formato não reconhecido")


def buscar_json(url: str):
    """Baixa e decodifica um JSON/JWS do TSE. Devolve None em caso de erro,
    sem levantar exceção (para o loop continuar tentando na próxima rodada)."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEGUNDOS) as resp:
            return decodificar_corpo(resp.read())
    except urllib.error.HTTPError as e:
        print(f"  [erro HTTP {e.code}] {url}")
    except urllib.error.URLError as e:
        print(f"  [erro de conexão] {url} — {e.reason}")
    except ValueError:
        print(f"  [erro] resposta não é JSON/JWS válido: {url}")
    return None


def urls_cargo(cargo_codigo: str, eleicao: str) -> list:
    """Monta candidatos de URL do arquivo de resultado por cargo, no padrão
    real do TSE (confirmado comparando com dados oficiais já publicados):

        <base>/<pasta>/dados-simplificados/<uf>/<uf>-c<cargo:04d>-e<eleicao:06d>-r.json

    Tenta primeiro o ambiente de PRODUÇÃO com os códigos confirmados
    (pleito 3220, eleição 6257/6259 conforme o cargo). Se ainda não estiver
    ativo (dá 404 até o TSE ligar o ambiente, normalmente perto do dia da
    eleição), cai para o ambiente de SIMULAÇÃO como fallback, para você
    poder continuar testando o script antes do dia real."""
    cargo4 = cargo_codigo.zfill(4)
    eleicao6 = eleicao.zfill(6)
    candidatos = []

    # 0) Produção 2026 — padrão REAL visto no site do TSE:
    #    /oficial/ele2026/<eleicao>/dados/<uf>/<uf>-c<cargo>-e<eleicao>-u.jws
    for ext in ("u.jws", "r.jws", "u.json"):
        candidatos.append(
            f"{BASE_URL}/ele2026/{eleicao}/dados/{UF}/{UF}-c{cargo4}-e{eleicao6}-{ext}"
        )
    candidatos.append(
        f"{BASE_URL}/ele2026/{eleicao}/dados-simplificados/{UF}/{UF}-c{cargo4}-e{eleicao6}-r.json"
    )

    # 1) Produção — pasta = pleito (padrão confirmado)
    candidatos.append(
        f"{BASE_URL}/{PLEITO}/dados-simplificados/{UF}/{UF}-c{cargo4}-e{eleicao6}-r.json"
    )
    # 2) Produção — pasta = eleição (variação, caso o TSE use esse formato)
    candidatos.append(
        f"{BASE_URL}/{eleicao}/dados-simplificados/{UF}/{UF}-c{cargo4}-e{eleicao6}-r.json"
    )
    # 3) Simulação — fallback para continuar testando antes do dia real
    eleicao_sim6 = ELEICAO_SIMULADO.zfill(6)
    for pasta in (ELEICAO_SIMULADO, PLEITO_SIMULADO):
        candidatos.append(
            f"{BASE_URL_SIMULADO}/{pasta}/dados-simplificados/{UF}/{UF}-c{cargo4}-e{eleicao_sim6}-r.json"
        )
    # 4) padrão antigo, por garantia
    candidatos.append(
        f"{BASE_URL_SIMULADO}/{ELEICAO_SIMULADO}/dados/{UF}/{UF}-c{cargo_codigo}-e{ELEICAO_SIMULADO}-u.json"
    )

    # remove duplicatas mantendo a ordem
    vistos = set()
    unicos = []
    for u in candidatos:
        if u not in vistos:
            vistos.add(u)
            unicos.append(u)
    return unicos


def _achar_lista_candidatos(no):
    """Junta TODAS as listas de candidatos ('nm' + 'vap') encontradas em qualquer
    nível do JSON (ex.: uma lista por partido) numa só, sem duplicar o mesmo
    candidato. Aceita variações do formato novo (.jws) do TSE."""
    achadas = []

    def varrer(x):
        if isinstance(x, list):
            if x and all(isinstance(i, dict) for i in x) and any("nm" in i and "vap" in i for i in x):
                achadas.extend(i for i in x if "nm" in i)
            else:
                for i in x:
                    varrer(i)
        elif isinstance(x, dict):
            for v in x.values():
                varrer(v)

    varrer(no)
    vistos = {}
    for c in achadas:
        chave = str(c.get("sqcand") or c.get("sq") or c.get("n") or "") + "|" + str(c.get("nm"))
        try:
            v = int(c.get("vap") or 0)
        except (TypeError, ValueError):
            v = 0
        if chave not in vistos or v > int(vistos[chave].get("vap") or 0):
            vistos[chave] = c
    return list(vistos.values()) or None


def extrair_candidatos(payload) -> list:
    """Converte o JSON bruto do TSE numa lista [{nome, partido, votos}, ...].

    Confirmado contra dados reais já publicados pelo TSE (eleição 2022): os
    candidatos vêm numa lista achatada em payload["cand"], e cada item usa
    'nm' para o nome, 'cc' para a sigla do partido/coligação e 'vap' para os
    votos. Ex.: {"nm":"DELEGADOR INACIO","cc":"PDT","vap":"14163", ...}.
    Mantemos as chaves alternativas como fallback, caso o ambiente de
    simulação use uma variação diferente.
    """
    candidatos = []
    if not payload:
        return candidatos

    # Formato comum: payload["cand"] é uma lista de candidatos
    lista = payload.get("cand") if isinstance(payload, dict) else None
    if lista is None and isinstance(payload, list):
        lista = payload
    todos = _achar_lista_candidatos(payload)
    if todos and (not lista or len(todos) > len(lista)):
        lista = todos

    if not lista:
        return candidatos

    for c in lista:
        nome = c.get("nm") or c.get("nome") or c.get("nmu") or "—"
        partido_raw = (
            c.get("cc")           # confirmado: sigla do partido/coligação
            or c.get("sg")
            or c.get("partido")
            or (c.get("agr", {}) or {}).get("sg")
            or "—"
        )
        # 'cc' às vezes vem como "PT - Federação Brasil da Esperança...";
        # nesse caso mostramos só a sigla antes do " - " para caber no painel.
        partido = partido_raw.split(" - ")[0].strip() if isinstance(partido_raw, str) else partido_raw
        votos_raw = c.get("vap") or c.get("votos") or c.get("qtde") or 0
        try:
            votos = int(votos_raw)
        except (TypeError, ValueError):
            votos = 0
        item = {"nome": nome, "partido": partido, "votos": votos}
        num = c.get("n") or c.get("nr") or c.get("numero")
        sq = c.get("sqcand") or c.get("sq")
        if num:
            item["num"] = str(num)
        if sq:
            item["sq"] = str(sq)
        candidatos.append(item)

    return candidatos


UF_ESPERADA = "AP"  # Amapá — trava de segurança contra dado de outro estado

# Campos onde o TSE costuma indicar a sigla do estado/abrangência do arquivo.
# Nem todo arquivo tem esses campos — quando nenhum aparece, não dá para
# confirmar, então seguimos em frente (mas o "ap" já está fixo na própria
# URL, então o risco de vir outro estado é baixo).
CAMPOS_UF_PAYLOAD = ("uf", "sg_uf", "sguf", "esae", "abr")


UFS_BR = {"AC","AL","AP","AM","BA","CE","DF","ES","GO","MA","MT","MS","MG","PA","PB","PR",
          "PE","PI","RJ","RN","RS","RO","RR","SC","SP","SE","TO","BR","ZZ"}


def confere_uf(payload) -> bool | None:
    """Confere se o arquivo é do Amapá. Só BLOQUEIA (False) quando acha, em
    campos de UF, uma sigla de estado válida que não seja AP. Valores que não
    são sigla de UF (ex.: 's', 'n', códigos de agrupamento) são ignorados —
    a UF já está fixa no endereço (/ap/). Mostra no log o campo usado."""
    if not isinstance(payload, dict):
        return None
    for campo in ("uf", "sg_uf", "sguf", "cdabr", "abr", "esae"):
        valor = payload.get(campo)
        if isinstance(valor, str) and valor.strip().upper() in UFS_BR:
            ok = valor.strip().upper() == UF_ESPERADA
            if not ok:
                print(f"  [aviso] campo '{campo}' = '{valor}' (esperado AP)")
            return ok
    return None


# ---------------------------------------------------------------------------
# Fotos dos candidatos (Portal de Dados Abertos do TSE — opcional, ver --fotos)
# ---------------------------------------------------------------------------
# Fonte: dadosabertos.tse.jus.br/dataset/candidatos-2026 — o mesmo portal
# oficial de onde saem as fotos usadas no site do TSE. Dois arquivos:
#   1) "Candidatos" (CSV, todas as UFs) — tem o número sequencial de cada
#      candidato (SQ_CANDIDATO), nome de urna, cargo e partido.
#   2) "AP - Fotos de candidatos" (ZIP) — as fotos em .jpg do Amapá; cada
#      arquivo dentro do zip tem o SQ_CANDIDATO no nome.
# Cruzando os dois dá pra ligar nome -> SQ_CANDIDATO -> arquivo de foto.
# (Confirmado contra o formato usado por outros projetos abertos que já
# processam esses mesmos arquivos do TSE.)
DADOS_ABERTOS_CAND_ZIP_URL = "https://cdn.tse.jus.br/estatistica/sead/odsele/consulta_cand/consulta_cand_2026.zip"
DADOS_ABERTOS_FOTOS_ZIP_URL = f"https://cdn.tse.jus.br/estatistica/sead/eleicoes/eleicoes2026/fotos/foto_cand2026_{UF.upper()}_div.zip"

FOTOS_DIR = Path(__file__).parent / "fotos"
FOTOS_MAPA_JSON = Path(__file__).parent / "fotos_mapa.json"

# Códigos de cargo (CD_CARGO) no CSV do TSE — os mesmos números já usados
# em CARGOS[...]["codigo"] acima (3=Governador, 5=Senador, 6=Dep. Federal,
# 7=Dep. Estadual), então basta reaproveitar.
CARGOS_CODIGOS = {info["codigo"] for info in CARGOS.values()}


def normalizar_nome(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.upper().strip()


def baixar_bytes(url: str) -> bytes | None:
    """Baixa um arquivo binário (zip). Devolve None em caso de erro, sem
    levantar exceção."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        print(f"  [fotos] [erro HTTP {e.code}] {url}")
    except urllib.error.URLError as e:
        print(f"  [fotos] [erro de conexão] {url} — {e.reason}")
    return None


def carregar_candidatos_oficiais_ap() -> list:
    """Baixa o CSV oficial de candidatos (Dados Abertos do TSE) e devolve
    só as linhas do Amapá para os cargos que o painel mostra, como lista
    de dicts {sq_candidato, nome_urna, cargo_codigo}."""
    print(f"  [fotos] baixando lista oficial de candidatos ({DADOS_ABERTOS_CAND_ZIP_URL})...")
    bruto = baixar_bytes(DADOS_ABERTOS_CAND_ZIP_URL)
    if not bruto:
        return []
    try:
        zf = zipfile.ZipFile(io.BytesIO(bruto))
    except zipfile.BadZipFile:
        print("  [fotos] o arquivo baixado não é um zip válido — pulando fotos.")
        return []

    # O zip traz um CSV por UF (ex.: consulta_cand_2026_AP.csv). Procura o
    # do Amapá pelo sufixo do nome, sem depender do nome exato da pasta.
    alvo = f"_{UF.upper()}.CSV"
    nome_arquivo = next((n for n in zf.namelist() if n.upper().endswith(alvo)), None)
    if not nome_arquivo:
        print(f"  [fotos] não achei o CSV do Amapá dentro do zip (procurado: *{alvo}).")
        return []

    conteudo = zf.read(nome_arquivo)
    # CSVs do TSE normalmente vêm em latin-1 e separados por ';'.
    texto = conteudo.decode("latin-1", errors="replace")
    leitor = csv.DictReader(io.StringIO(texto), delimiter=";")

    candidatos = []
    for linha in leitor:
        cargo_codigo = (linha.get("CD_CARGO") or "").strip()
        if cargo_codigo not in CARGOS_CODIGOS:
            continue
        sq = (linha.get("SQ_CANDIDATO") or "").strip()
        nome_urna = (linha.get("NM_URNA_CANDIDATO") or linha.get("NM_CANDIDATO") or "").strip()
        if not sq or not nome_urna:
            continue
        candidatos.append({
            "sq_candidato": sq, "nome_urna": nome_urna, "cargo_codigo": cargo_codigo,
            "nome_completo": (linha.get("NM_CANDIDATO") or "").strip(),
            "numero": (linha.get("NR_CANDIDATO") or "").strip(),
        })
    print(f"  [fotos] {len(candidatos)} candidatos do Amapá encontrados no CSV oficial.")
    return candidatos


def baixar_fotos_uma_vez() -> dict:
    """Baixa o CSV de candidatos + o zip de fotos do Amapá, extrai as fotos
    dos candidatos relevantes para fotos/<sq_candidato>.jpg e devolve um
    mapa {nome_normalizado: 'fotos/<sq_candidato>.jpg'}. Em qualquer falha,
    devolve {} e deixa o painel usando as iniciais — nunca derruba o
    script principal."""
    candidatos = carregar_candidatos_oficiais_ap()
    if not candidatos:
        return {}
    sqs_validos = {c["sq_candidato"] for c in candidatos}
    nome_por_sq = {c["sq_candidato"]: c["nome_urna"] for c in candidatos}

    print(f"  [fotos] baixando pacote de fotos do Amapá ({DADOS_ABERTOS_FOTOS_ZIP_URL})...")
    bruto_fotos = baixar_bytes(DADOS_ABERTOS_FOTOS_ZIP_URL)
    if not bruto_fotos:
        return {}
    try:
        zf = zipfile.ZipFile(io.BytesIO(bruto_fotos))
    except zipfile.BadZipFile:
        print("  [fotos] pacote de fotos baixado não é um zip válido.")
        return {}

    FOTOS_DIR.mkdir(exist_ok=True)
    mapa = {}
    for entry in zf.infolist():
        if entry.is_dir() or not re.search(r"\.(jpe?g|png)$", entry.filename, re.I):
            continue
        # O nome do arquivo dentro do zip traz o SQ_CANDIDATO em algum
        # trecho numérico com 6+ dígitos — mesma convenção usada por
        # outros projetos que já processam esse mesmo pacote do TSE.
        achados = re.findall(r"\d{6,}", entry.filename)
        sq = next((d for d in achados if d in sqs_validos), None)
        if not sq:
            continue
        destino = FOTOS_DIR / f"{sq}.jpg"
        destino.write_bytes(zf.read(entry))
        mapa[normalizar_nome(nome_por_sq[sq])] = f"fotos/{sq}.jpg"

    print(f"  [fotos] {len(mapa)}/{len(candidatos)} fotos extraídas e salvas em {FOTOS_DIR}/")
    FOTOS_MAPA_JSON.write_text(json.dumps(mapa, ensure_ascii=False, indent=2), encoding="utf-8")
    return mapa


def carregar_mapa_fotos() -> dict:
    """Lê o mapa já salvo em fotos_mapa.json, sem baixar nada da internet.
    Usado nas rodadas normais do --loop para não repetir o download do
    zip de fotos a cada 5 minutos."""
    if FOTOS_MAPA_JSON.exists():
        try:
            return json.loads(FOTOS_MAPA_JSON.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


CANDIDATOS_AP_JSON = Path(__file__).parent / "candidatos_ap.json"

_PARTICULAS = {"DA", "DE", "DO", "DAS", "DOS", "E"}


def _nome_bonito(nome: str) -> str:
    """'JAIME PEREZ' -> 'Jaime Perez'; 'DR. FURLAN' -> 'Dr. Furlan'."""
    partes = []
    for i, p in enumerate(nome.split()):
        partes.append(p.lower() if (i > 0 and p.upper() in _PARTICULAS) else p[:1].upper() + p[1:].lower())
    return " ".join(partes)


def carregar_tabela_candidatos() -> list:
    """Tabela oficial (nome de urna, nome completo, número, SQ) do Amapá.
    Fica em candidatos_ap.json para não baixar o CSV a cada rodada."""
    if CANDIDATOS_AP_JSON.exists():
        try:
            tab = json.loads(CANDIDATOS_AP_JSON.read_text(encoding="utf-8"))
            if tab:
                return tab
        except json.JSONDecodeError:
            pass
    tab = carregar_candidatos_oficiais_ap()
    if tab:
        CANDIDATOS_AP_JSON.write_text(json.dumps(tab, ensure_ascii=False), encoding="utf-8")
    return tab


def harmonizar_nomes(candidatos: list, cargo_codigo: str, tabela: list, silencioso: bool = False) -> None:
    """O arquivo novo do TSE traz o nome completo; o painel usa o NOME DE URNA
    (o mesmo das fotos). Casa por nome completo ou pelo número do candidato e
    troca para o nome de urna. Quem não casar fica como veio."""
    if not tabela:
        return
    por_nome = {(t["cargo_codigo"], normalizar_nome(t["nome_completo"])): t for t in tabela if t.get("nome_completo")}
    por_num = {(t["cargo_codigo"], t["numero"]): t for t in tabela if t.get("numero")}
    por_urna = {(t["cargo_codigo"], normalizar_nome(t["nome_urna"])): t for t in tabela}
    trocados = 0
    for c in candidatos:
        t = (por_nome.get((cargo_codigo, normalizar_nome(c["nome"])))
             or por_urna.get((cargo_codigo, normalizar_nome(c["nome"])))
             or (por_num.get((cargo_codigo, c.get("num"))) if c.get("num") else None))
        if t:
            c["nome"] = _nome_bonito(t["nome_urna"])
            trocados += 1
    if not silencioso:
        print(f"  [nomes] {trocados}/{len(candidatos)} candidatos casados com o nome de urna oficial")


def aplicar_fotos(candidatos: list, mapa: dict) -> None:
    """Preenche candidatos[i]['foto'] a partir do mapa já carregado (sem
    acessar a rede). Uma falha de correspondência simplesmente deixa o
    candidato sem foto (painel usa iniciais)."""
    if not mapa:
        return
    achadas = 0
    for c in candidatos:
        caminho = mapa.get(normalizar_nome(c["nome"]))
        if caminho:
            c["foto"] = caminho
            achadas += 1
    print(f"  [fotos] {achadas}/{len(candidatos)} candidatos com foto aplicada")


def _diagnostico(payload, nivel=0, limite=2):
    """Mostra no log as chaves do arquivo (para ajustar o formato se preciso)."""
    if nivel == 0:
        print(f"  [diagnóstico] tipo={type(payload).__name__}")
    if isinstance(payload, dict):
        resumo = {k: (type(v).__name__ if isinstance(v, (dict, list)) else str(v)[:40]) for k, v in list(payload.items())[:25]}
        print("  " * (nivel + 1) + f"[diagnóstico] chaves: {resumo}")
        if nivel < limite:
            for v in payload.values():
                if isinstance(v, (dict, list)):
                    _diagnostico(v, nivel + 1, limite)
                    break
    elif isinstance(payload, list) and payload:
        print("  " * (nivel + 1) + f"[diagnóstico] lista com {len(payload)} itens")
        if nivel < limite:
            _diagnostico(payload[0], nivel + 1, limite)


def buscar_cargo(chave: str, info: dict) -> list | None:
    print(f"Buscando {info['label']} (filtro: apenas Amapá)...")
    for url in urls_cargo(info["codigo"], info["eleicao"]):
        print(f"  tentando {url}")
        payload = buscar_json(url)
        if payload is None:
            continue

        eh_amapa = confere_uf(payload)
        if eh_amapa is False:
            print(f"  [bloqueado] essa URL devolveu dados de outro estado "
                  f"(esperado: {UF_ESPERADA}) — ignorando, tentando a próxima.")
            continue

        candidatos = extrair_candidatos(payload)
        if not candidatos:
            print(f"  [aviso] essa URL respondeu, mas sem candidatos "
                  f"reconhecíveis no formato esperado — tentando a próxima.")
            _diagnostico(payload)
            continue
        if isinstance(payload, dict):
            print("  [estrutura] chaves do arquivo: " + ", ".join(
                f"{k}={type(v).__name__}{'['+str(len(v))+']' if isinstance(v,(list,dict)) else ''}"
                for k, v in list(payload.items())[:20]))
        print(f"  ok ({url}) — {len(candidatos)} candidatos, "
              f"{sum(c['votos'] for c in candidatos)} votos totais"
              f"{' [UF confirmada: AP]' if eh_amapa else ' [UF não confirmada no payload, mas URL já é de AP]'}")
        return candidatos
    print(f"  [erro] nenhuma das URLs testadas funcionou para {info['label']}.")
    return None


def urls_cargo_municipio(cod_mun: str, cargo_codigo: str, eleicao: str) -> list:
    """URLs do arquivo de resultado de UM município (padrão do TSE:
    <uf><cod_municipio>-c<cargo>-e<eleicao>-r.json, ex.: ap06050-c0006-e006257-r.json)."""
    cargo4, eleicao6 = cargo_codigo.zfill(4), eleicao.zfill(6)
    nome = f"{UF}{cod_mun}-c{cargo4}-e{eleicao6}-r.json"
    base_mun = f"{UF}{cod_mun}-c{cargo4}-e{eleicao6}"
    return [
        f"{BASE_URL}/ele2026/{eleicao}/dados/{UF}/{base_mun}-u.jws",
        f"{BASE_URL}/ele2026/{eleicao}/dados/{UF}/{base_mun}-r.jws",
        f"{BASE_URL}/ele2026/{eleicao}/dados-simplificados/{UF}/{nome}",
        f"{BASE_URL}/ele2026/{PLEITO}/dados-simplificados/{UF}/{nome}",
        f"{BASE_URL}/{PLEITO}/dados-simplificados/{UF}/{nome}",
        f"{BASE_URL}/{eleicao}/dados-simplificados/{UF}/{nome}",
        f"{BASE_URL}/{PLEITO}/dados-simplificados/{UF}/{cod_mun}-c{cargo4}-e{eleicao6}-r.json",
    ]


def buscar_votos_municipios(dados: dict) -> dict:
    """Para cada cargo e cada um dos 16 municípios, baixa o arquivo municipal do
    TSE e monta {cargo: {nome_candidato: {municipio: votos}}}. O nome do
    candidato é o MESMO que está no dados.json (casado sem acento/maiúscula).
    Municípios que falharem ficam de fora desta rodada (não derruba o resto)."""
    resultado = {}
    for chave, info in CARGOS.items():
        candidatos = (dados.get("racas", {}).get(chave) or {}).get("candidatos") or []
        if not candidatos:
            continue
        por_nome = {normalizar_nome(c["nome"]): c["nome"] for c in candidatos}
        por_num = {c["num"]: c["nome"] for c in candidatos if c.get("num")}
        tabela = carregar_tabela_candidatos()
        saida = {}
        ok = 0
        for cod, nome_mun in MUNICIPIOS.items():
            payload = None
            for url in urls_cargo_municipio(cod, info["codigo"], info["eleicao"]):
                payload = buscar_json(url)
                if payload is not None:
                    break
            if payload is None:
                continue
            ok += 1
            lista_mun = extrair_candidatos(payload)
            # o arquivo municipal também traz o nome completo: converte para o
            # nome de urna (o mesmo do painel) antes de casar
            harmonizar_nomes(lista_mun, info["codigo"], tabela, silencioso=True)
            for c in lista_mun:
                nome = por_nome.get(normalizar_nome(c["nome"])) or (por_num.get(c.get("num")) if c.get("num") else None)
                if nome:
                    saida.setdefault(nome, {})[nome_mun] = c["votos"]
        print(f"  [municípios] {info['label']}: {ok}/{len(MUNICIPIOS)} municípios lidos, "
              f"{len(saida)}/{len(candidatos)} candidatos com votos por município")
        if saida:
            resultado[chave] = saida
    return resultado


def buscar_municipios() -> list | None:
    """Busca o percentual de seções apuradas por município.
    Usa o arquivo agregado por cargo (qualquer um serve, todos têm o
    mesmo total de seções apuradas por município) e extrai o campo de
    progresso. Ajuste a chave 'pst'/'perc' conforme a estrutura real.

    Aviso: no arquivo agregado por UF, o TSE normalmente NÃO traz o
    detalhamento por município (esse vem em arquivos separados, um por
    município). Esta função tenta achar um bloco 'mu' mesmo assim; se não
    encontrar, os percentuais por município ficam como estavam (0% no
    início) até essa parte ser ajustada para buscar os 16 arquivos
    municipais individualmente."""
    payload = None
    for url in urls_cargo(CARGOS["governador"]["codigo"], CARGOS["governador"]["eleicao"]):
        candidato = buscar_json(url)
        if candidato is None:
            continue
        if confere_uf(candidato) is False:
            print(f"  [bloqueado] {url} devolveu dados de outro estado — ignorando.")
            continue
        payload = candidato
        break
    if payload is None:
        return None

    municipios_payload = payload.get("mu") if isinstance(payload, dict) else None
    if not municipios_payload:
        print("  [aviso] não encontrei bloco de municípios no JSON do cargo "
              "governador — mantendo dados de município anteriores.")
        return None

    resultado = []
    for m in municipios_payload:
        codigo = m.get("cd") or m.get("codigo")
        pct_raw = m.get("pst") or m.get("perc") or m.get("pct") or 0
        try:
            pct = round(float(pct_raw))
        except (TypeError, ValueError):
            pct = 0
        nome = MUNICIPIOS.get(str(codigo), m.get("nm", f"Município {codigo}"))
        resultado.append({"nome": nome, "pct": pct})

    # Garante que os 16 municípios apareçam mesmo que algum não tenha
    # vindo no payload (fica com 0%).
    encontrados = {r["nome"] for r in resultado}
    for nome in MUNICIPIOS.values():
        if nome not in encontrados:
            resultado.append({"nome": nome, "pct": 0})

    return resultado


# ---------------------------------------------------------------------------
# Gravação do dados.json
# ---------------------------------------------------------------------------

def carregar_dados_atuais() -> dict:
    if DADOS_JSON.exists():
        try:
            return json.loads(DADOS_JSON.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"racas": {}, "municipios": [], "atualizadoEm": None}


def rodar_uma_vez(buscar_fotos: bool = False, votos_municipio: bool = False):
    dados = carregar_dados_atuais()
    algo_mudou = False

    mapa_fotos = {}
    if buscar_fotos:
        mapa_fotos = carregar_mapa_fotos()
        if not mapa_fotos:
            # primeira vez: baixa CSV + zip de fotos e grava o cache local
            mapa_fotos = baixar_fotos_uma_vez()

    for chave, info in CARGOS.items():
        candidatos = buscar_cargo(chave, info)
        if candidatos is not None and buscar_fotos:
            harmonizar_nomes(candidatos, info["codigo"], carregar_tabela_candidatos())
        if candidatos is not None:
            antigo = (dados["racas"].get(chave) or {}).get("candidatos") or []
            tot_antigo = sum(int(c.get("votos") or 0) for c in antigo)
            tot_novo = sum(int(c.get("votos") or 0) for c in candidatos)
            if tot_antigo > 0 and tot_novo < tot_antigo:
                print(f"  [proteção] {info['label']}: leitura nova ({tot_novo} votos) é menor que a "
                      f"já salva ({tot_antigo}) — mantendo os dados salvos.")
                continue
            dados["racas"][chave] = {
                "label": info["label"],
                "seatsLabel": f"{info['seats']} vaga{'s' if info['seats'] != 1 else ''}",
                "seats": info["seats"],
                "proportional": info["proportional"],
                "candidatos": candidatos,
            }
            algo_mudou = True

    municipios = buscar_municipios()
    if municipios is not None:
        dados["municipios"] = municipios
        algo_mudou = True

    # Aplica as fotos aos candidatos que já estão no dados.json (sejam eles
    # recém-buscados nesta rodada ou os que já estavam lá antes — por
    # exemplo, a lista com nomes reais e 0 votos, enquanto o TSE ainda não
    # liberou a apuração). Isso garante que a foto apareça mesmo quando a
    # busca de resultados dá 404 o tempo todo antes do dia da eleição.
    if votos_municipio:
        vm = buscar_votos_municipios(dados)
        if vm:
            dados.setdefault("votosMunicipio", {}).update(vm)
            algo_mudou = True

    if buscar_fotos and mapa_fotos:
        for chave, raca in dados["racas"].items():
            candidatos = raca.get("candidatos") or []
            antes = sum(1 for c in candidatos if c.get("foto"))
            aplicar_fotos(candidatos, mapa_fotos)
            depois = sum(1 for c in candidatos if c.get("foto"))
            if depois > antes:
                algo_mudou = True

    if algo_mudou:
        dados["atualizadoEm"] = datetime.now(timezone.utc).isoformat()
        DADOS_JSON.write_text(
            json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"✔ dados.json atualizado em {dados['atualizadoEm']}\n")
    else:
        print("✘ Nada foi atualizado nesta rodada (todas as buscas falharam). "
              "dados.json mantido como estava.\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="roda uma única vez e sai")
    parser.add_argument("--loop", action="store_true", help="roda em loop continuamente")
    parser.add_argument("--intervalo", type=int, default=300,
                         help="segundos entre rodadas no modo --loop (padrão: 300 = 5 min)")
    parser.add_argument("--base-url", type=str, default=None,
                         help="sobrescreve BASE_URL (use a URL de produção do TSE)")
    parser.add_argument("--fotos", action="store_true",
                         help="baixa (uma vez) e aplica as fotos oficiais do TSE a cada candidato — ver item 5 do topo do arquivo")
    parser.add_argument("--votos-municipio", action="store_true",
                         help="baixa os votos de cada candidato por município (aba 'Votos por município' do painel)")
    args = parser.parse_args()

    global BASE_URL
    if args.base_url:
        BASE_URL = args.base_url.rstrip("/")

    if not args.once and not args.loop:
        print("Use --once para rodar uma vez, ou --loop para rodar continuamente.")
        sys.exit(1)

    if args.once:
        rodar_uma_vez(buscar_fotos=args.fotos, votos_municipio=args.votos_municipio)
        return

    print(f"Rodando em loop, a cada {args.intervalo}s. Ctrl+C para parar.\n")
    try:
        while True:
            rodar_uma_vez(buscar_fotos=args.fotos, votos_municipio=args.votos_municipio)
            time.sleep(args.intervalo)
    except KeyboardInterrupt:
        print("\nEncerrado pelo usuário.")


if __name__ == "__main__":
    main()
