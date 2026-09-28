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
import json
import sys
import time
import urllib.request
import urllib.error
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
    "senador":      {"codigo": "5", "eleicao": "6257", "label": "Senador",            "proportional": False, "seats": 2},
    "dep_federal":  {"codigo": "6", "eleicao": "6257", "label": "Deputado Federal",   "proportional": True,  "seats": 8},
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

def buscar_json(url: str):
    """Baixa e decodifica um JSON do TSE. Devolve None em caso de erro,
    sem levantar exceção (para o loop continuar tentando na próxima rodada)."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEGUNDOS) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"  [erro HTTP {e.code}] {url}")
    except urllib.error.URLError as e:
        print(f"  [erro de conexão] {url} — {e.reason}")
    except json.JSONDecodeError:
        print(f"  [erro] resposta não é um JSON válido: {url}")
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
        candidatos.append({"nome": nome, "partido": partido, "votos": votos})

    return candidatos


UF_ESPERADA = "AP"  # Amapá — trava de segurança contra dado de outro estado

# Campos onde o TSE costuma indicar a sigla do estado/abrangência do arquivo.
# Nem todo arquivo tem esses campos — quando nenhum aparece, não dá para
# confirmar, então seguimos em frente (mas o "ap" já está fixo na própria
# URL, então o risco de vir outro estado é baixo).
CAMPOS_UF_PAYLOAD = ("uf", "sg_uf", "sguf", "esae", "abr")


def confere_uf(payload) -> bool | None:
    """Confere se o JSON baixado é mesmo do Amapá, olhando os campos que o
    TSE costuma usar para indicar a sigla do estado. Retorna True/False
    quando consegue confirmar, ou None quando o arquivo não tem nenhum
    desses campos (nesse caso não bloqueamos — a UF já está fixa na URL)."""
    if not isinstance(payload, dict):
        return None
    for campo in CAMPOS_UF_PAYLOAD:
        valor = payload.get(campo)
        if isinstance(valor, str) and len(valor) <= 3:
            return valor.strip().upper() == UF_ESPERADA
    return None


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
            continue
        print(f"  ok ({url}) — {len(candidatos)} candidatos, "
              f"{sum(c['votos'] for c in candidatos)} votos totais"
              f"{' [UF confirmada: AP]' if eh_amapa else ' [UF não confirmada no payload, mas URL já é de AP]'}")
        return candidatos
    print(f"  [erro] nenhuma das URLs testadas funcionou para {info['label']}.")
    return None


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


def rodar_uma_vez():
    dados = carregar_dados_atuais()
    algo_mudou = False

    for chave, info in CARGOS.items():
        candidatos = buscar_cargo(chave, info)
        if candidatos is not None:
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
    args = parser.parse_args()

    global BASE_URL
    if args.base_url:
        BASE_URL = args.base_url.rstrip("/")

    if not args.once and not args.loop:
        print("Use --once para rodar uma vez, ou --loop para rodar continuamente.")
        sys.exit(1)

    if args.once:
        rodar_uma_vez()
        return

    print(f"Rodando em loop, a cada {args.intervalo}s. Ctrl+C para parar.\n")
    try:
        while True:
            rodar_uma_vez()
            time.sleep(args.intervalo)
    except KeyboardInterrupt:
        print("\nEncerrado pelo usuário.")


if __name__ == "__main__":
    main()
