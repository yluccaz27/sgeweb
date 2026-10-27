#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SGE - Equipe 5: Lista de Chamada -> Certificado -> Crachá   (Python + Flask)

Simula o MESMO app Android (as 3 telas, o mesmo fluxo e as mesmas regras).
Dois arquivos, na mesma pasta:
    sge_app.py   back-end: API fake + geração do PDF/PNG + serve o index.html
    index.html   front-end: as 3 telas (presença -> certificado -> crachá), CSS e JS
O app Android também consegue chamar essas rotas de verdade.

Como rodar
    pip install flask pillow
    python sge_app.py                      -> http://127.0.0.1:5000
    python sge_app.py --host 0.0.0.0       -> aceita conexões da rede (celular / emulador)

Dá pra mexer no visual só editando o index.html (basta recarregar a página, sem reiniciar).
Também dá pra abrir o index.html direto do disco com este servidor rodando.

Opcional: coloque um arquivo "logo.png" na mesma pasta deste script e ele vira a imagem do
topo das telas, do certificado e do crachá. Sem ele, usa uma logo provisória.
Os PDFs e PNGs gerados ficam na pasta "saidas/" (como o app salva no celular).

Rotas da API (as mesmas do PDF da faculdade)
    GET  /eventos/<id>/chamada          lista de chamada
    PUT  /chamada/<id>/presenca         corpo: {"presente": true}
    GET  /modelos/evento/<id>           modelo do evento
    GET  /modelos/curso/<id>            modelo do curso
    POST /certificados                  gera o PDF   |  GET /certificados/<id>  baixa o PDF
    POST /crachas                       gera o PNG   |  GET /crachas/<id>       baixa o PNG
"""
import argparse
import io
import os
import time

from flask import Flask, jsonify, request, send_file
from PIL import Image, ImageDraw, ImageFont

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PASTA_SAIDAS = os.path.join(BASE_DIR, "saidas")
ARQUIVO_LOGO = os.path.join(BASE_DIR, "logo.png")
os.makedirs(PASTA_SAIDAS, exist_ok=True)

LARANJA = (239, 108, 0)
BRANCO = (255, 255, 255)
PRETO = (0, 0, 0)
CINZA_ESCURO = (68, 68, 68)
LANCZOS = getattr(Image, "Resampling", Image).LANCZOS

app = Flask(__name__)
try:
    app.json.ensure_ascii = False
except AttributeError:  # Flask antigo
    app.config["JSON_AS_ASCII"] = False


# =====================================================================================
#  BANCO FAKE (em memória: reinicia quando o servidor reinicia, igual ao app)
# =====================================================================================
def _participante(id_chamada, id_participante, nome, presente):
    return {"id": id_chamada, "id_participante": id_participante,
            "nome_participante": nome, "presente": presente}


CHAMADA = {
    1: [
        _participante(1, 101, "João Silva", False),
        _participante(2, 102, "Maria Souza", False),
        _participante(3, 103, "Carlos Andrade", False),
        _participante(4, 104, "Ana Paula Ribeiro", True),
        _participante(5, 105, "Pedro Henrique", False),
        _participante(6, 106, "Fernanda Lima", True),
        _participante(7, 107, "Rafael Rodrigues", False),
        _participante(8, 108, "Vitória Cristina", False),
    ]
}
EVENTOS = {1: {"id": 1, "nome": "Evento X", "curso_id": 1}}
TIPOS_CERTIFICADO = ["Participação", "Palestrante", "Organizador"]
NOMES_MODELO = ["Modelo do evento", "Modelo do curso", "Modelo padrão"]
TEXTO_MODELO = ("Certificamos que {{nome}} participou do evento {{evento}}, "
                "do curso {{curso}}, em {{data}}.")
MODELOS = {
    "evento": {1: {"id": 1, "nome": "Modelo do evento", "texto": TEXTO_MODELO}},
    "curso": {1: {"id": 1, "nome": "Modelo do curso", "texto": TEXTO_MODELO}},
    "padrao": {"id": 0, "nome": "Modelo padrão", "texto": TEXTO_MODELO},
}
ARQUIVOS = {"certificados": {}, "crachas": {}}  # id -> caminho do arquivo gerado


def achar_chamada(id_chamada):
    for lista in CHAMADA.values():
        for c in lista:
            if c["id"] == id_chamada:
                return c
    return None


def resolver_modelo(id_evento):
    """Escolha em cascata: modelo do evento -> modelo do curso -> modelo padrão."""
    if id_evento in MODELOS["evento"]:
        return MODELOS["evento"][id_evento]
    curso = EVENTOS.get(id_evento, {}).get("curso_id")
    if curso in MODELOS["curso"]:
        return MODELOS["curso"][curso]
    return MODELOS["padrao"]


# =====================================================================================
#  IMAGENS: logo, certificado (PDF) e crachá (PNG)
# =====================================================================================
def fonte(tamanho, negrito=False):
    candidatos = (["arialbd.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf",
                   "/System/Library/Fonts/Helvetica.ttc"] if negrito else
                  ["arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf",
                   "/System/Library/Fonts/Helvetica.ttc"])
    for c in candidatos:
        try:
            return ImageFont.truetype(c, int(tamanho))
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=int(tamanho))
    except TypeError:
        return ImageFont.load_default()


def logo_placeholder(tam=240, fundo_escuro=False):
    k = 4  # desenha em 4x e reduz, pra borda ficar suave
    t = tam * k
    img = Image.new("RGBA", (t, t), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cor_circulo = BRANCO if fundo_escuro else LARANJA
    cor_check = LARANJA if fundo_escuro else BRANCO
    d.ellipse([4 * k, 4 * k, t - 4 * k, t - 4 * k], fill=cor_circulo)
    pts = [(t * 0.29, t * 0.52), (t * 0.44, t * 0.67), (t * 0.72, t * 0.35)]
    larg = max(4, tam // 12) * k
    d.line(pts, fill=cor_check, width=larg, joint="curve")
    for x, y in (pts[0], pts[-1]):  # pontas arredondadas
        d.ellipse([x - larg / 2, y - larg / 2, x + larg / 2, y + larg / 2], fill=cor_check)
    return img.resize((tam, tam), LANCZOS)


def carregar_logo(fundo_escuro=False):
    if os.path.exists(ARQUIVO_LOGO):
        try:
            return Image.open(ARQUIVO_LOGO).convert("RGBA")
        except OSError:
            pass
    return logo_placeholder(fundo_escuro=fundo_escuro)


def texto_centralizado(draw, x, y, txt, tamanho, negrito, cor, largura_max):
    """Desenha o texto centralizado na linha de base y, diminuindo a fonte se não couber."""
    while tamanho > 10 and draw.textlength(txt, font=fonte(tamanho, negrito)) > largura_max:
        tamanho -= 1
    draw.text((x, y), txt, font=fonte(tamanho, negrito), fill=cor, anchor="ms")


def gerar_pdf_certificado(tipo, modelo, participante, evento):
    """Mesmo layout do app: A4 paisagem (842x595), logo no topo e os textos centralizados."""
    esc = 2  # desenha em dobro e salva a 144 dpi: a página continua 842x595 pt, mas nítida
    largura, altura = 842, 595
    img = Image.new("RGB", (largura * esc, altura * esc), BRANCO)
    d = ImageDraw.Draw(img)

    logo = carregar_logo()
    lw = 120 * esc
    lh = int(logo.height * lw / logo.width)
    img.paste(logo.resize((lw, lh), LANCZOS), ((largura * esc - lw) // 2, 40 * esc),
              logo.resize((lw, lh), LANCZOS))

    centro = largura * esc / 2
    maxw = (largura - 80) * esc
    texto_centralizado(d, centro, 250 * esc, "CERTIFICADO DE " + tipo.upper(), 32 * esc, True, PRETO, maxw)
    texto_centralizado(d, centro, 300 * esc, "Certificamos que", 18 * esc, False, PRETO, maxw)
    texto_centralizado(d, centro, 345 * esc, participante, 26 * esc, True, PRETO, maxw)
    texto_centralizado(d, centro, 390 * esc, "participou do evento:", 18 * esc, False, PRETO, maxw)
    texto_centralizado(d, centro, 430 * esc, evento, 22 * esc, True, PRETO, maxw)
    texto_centralizado(d, centro, 470 * esc, "Modelo: " + modelo, 14 * esc, False, PRETO, maxw)

    nome = "certificado_%d.pdf" % int(time.time() * 1000)
    caminho = os.path.join(PASTA_SAIDAS, nome)
    img.save(caminho, "PDF", resolution=72 * esc)
    return nome, caminho


def gerar_png_cracha(participante, evento):
    """Mesmo layout do app: 600x900, faixa laranja com a logo, nome e evento centralizados."""
    img = Image.new("RGB", (600, 900), BRANCO)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 600, 180], fill=LARANJA)

    logo = carregar_logo(fundo_escuro=True)
    altura = 100
    largura = int(logo.width * altura / logo.height)
    logo = logo.resize((largura, altura), LANCZOS)
    img.paste(logo, ((600 - largura) // 2, 25), logo)

    texto_centralizado(d, 300, 350, participante.upper(), 34, True, PRETO, 540)
    texto_centralizado(d, 300, 450, evento, 28, False, CINZA_ESCURO, 540)

    nome = "cracha_%d.png" % int(time.time() * 1000)
    caminho = os.path.join(PASTA_SAIDAS, nome)
    img.save(caminho, "PNG")
    return nome, caminho


# =====================================================================================
#  API (mesmas rotas do PDF da faculdade)
# =====================================================================================
@app.get("/eventos/<int:id_evento>/chamada")
def api_lista_chamada(id_evento):
    if id_evento not in CHAMADA:
        return jsonify({"erro": "Evento não encontrado"}), 404
    return jsonify(CHAMADA[id_evento])


@app.put("/chamada/<int:id_chamada>/presenca")
def api_marcar_presenca(id_chamada):
    corpo = request.get_json(silent=True)
    # aceita {"presente": true} e também um boolean puro (o exemplo do PDF usava @Body Boolean)
    presente = corpo.get("presente") if isinstance(corpo, dict) else corpo
    if not isinstance(presente, bool):
        return jsonify({"erro": 'Envie {"presente": true} ou {"presente": false}'}), 400
    registro = achar_chamada(id_chamada)
    if registro is None:
        return jsonify({"erro": "Chamada não encontrada"}), 404
    registro["presente"] = presente
    return "", 204


@app.get("/modelos/evento/<int:id_evento>")
def api_modelo_evento(id_evento):
    modelo = MODELOS["evento"].get(id_evento)
    return (jsonify(modelo), 200) if modelo else (jsonify({"erro": "Sem modelo do evento"}), 404)


@app.get("/modelos/curso/<int:id_curso>")
def api_modelo_curso(id_curso):
    modelo = MODELOS["curso"].get(id_curso)
    return (jsonify(modelo), 200) if modelo else (jsonify({"erro": "Sem modelo do curso"}), 404)


@app.get("/modelos/resolver/<int:id_evento>")
def api_modelo_resolver(id_evento):
    return jsonify(resolver_modelo(id_evento))


@app.get("/api/opcoes")
def api_opcoes():
    return jsonify({"tipos": TIPOS_CERTIFICADO, "modelos": NOMES_MODELO})


@app.post("/certificados")
def api_criar_certificado():
    dados = request.get_json(silent=True) or {}
    participante = (dados.get("participante") or "").strip()
    evento = (dados.get("evento") or "").strip()
    tipo = dados.get("tipo") if dados.get("tipo") in TIPOS_CERTIFICADO else TIPOS_CERTIFICADO[0]
    if not participante or not evento:
        return jsonify({"erro": "Preencha o participante e o evento."}), 400

    # regra de negócio: certificado de participação só com presença validada
    id_participante = dados.get("participante_id")
    if tipo == "Participação" and id_participante:
        for lista in CHAMADA.values():
            for c in lista:
                if c["id_participante"] == id_participante and not c["presente"]:
                    return jsonify({"erro": "Certificado só pra quem tem presença validada"}), 403

    modelo = dados.get("modelo")
    if modelo not in NOMES_MODELO:
        modelo = resolver_modelo(dados.get("evento_id") or 0)["nome"]

    nome_arquivo, caminho = gerar_pdf_certificado(tipo, modelo, participante, evento)
    novo_id = len(ARQUIVOS["certificados"]) + 1
    ARQUIVOS["certificados"][novo_id] = caminho
    return jsonify({"id": novo_id, "tipo": tipo, "modelo": modelo,
                    "arquivo": "saidas/" + nome_arquivo, "url": "/certificados/%d" % novo_id}), 201


@app.get("/certificados/<int:id_certificado>")
def api_baixar_certificado(id_certificado):
    caminho = ARQUIVOS["certificados"].get(id_certificado)
    if not caminho:
        return jsonify({"erro": "Certificado não encontrado"}), 404
    return send_file(caminho, mimetype="application/pdf")


@app.post("/crachas")
def api_criar_cracha():
    dados = request.get_json(silent=True) or {}
    participante = (dados.get("participante") or "").strip()
    evento = (dados.get("evento") or "").strip()
    if not participante or not evento:
        return jsonify({"erro": "Preencha o evento e o participante!"}), 400
    nome_arquivo, caminho = gerar_png_cracha(participante, evento)
    novo_id = len(ARQUIVOS["crachas"]) + 1
    ARQUIVOS["crachas"][novo_id] = caminho
    return jsonify({"id": novo_id, "arquivo": "saidas/" + nome_arquivo,
                    "url": "/crachas/%d" % novo_id}), 201


@app.get("/crachas/<int:id_cracha>")
def api_baixar_cracha(id_cracha):
    caminho = ARQUIVOS["crachas"].get(id_cracha)
    if not caminho:
        return jsonify({"erro": "Crachá não encontrado"}), 404
    return send_file(caminho, mimetype="image/png")


@app.get("/logo")
def rota_logo():
    buf = io.BytesIO()
    carregar_logo().save(buf, "PNG")
    buf.seek(0)
    resposta = send_file(buf, mimetype="image/png")
    resposta.headers["Cache-Control"] = "no-store"
    return resposta


# =====================================================================================
#  FRONT-END: o index.html (as 3 telas) fica na mesma pasta deste script
# =====================================================================================
ARQUIVO_INDEX = os.path.join(BASE_DIR, "index.html")


@app.get("/")
def pagina_inicial():
    if not os.path.exists(ARQUIVO_INDEX):
        return "Faltou o index.html na mesma pasta do sge_app.py", 404
    resposta = send_file(ARQUIVO_INDEX, mimetype="text/html")
    resposta.headers["Cache-Control"] = "no-store"   # sempre pega a versão mais nova
    return resposta


@app.after_request
def liberar_cors(resposta):
    # permite abrir o index.html direto do disco (ou de outro servidor) falando com esta API
    resposta.headers["Access-Control-Allow-Origin"] = "*"
    resposta.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, OPTIONS"
    resposta.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resposta


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="SGE - Equipe 5 (Flask)")
    ap.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 pra liberar o acesso pela rede")
    ap.add_argument("--port", type=int, default=5000)
    args = ap.parse_args()
    print("\n  SGE - Equipe 5 rodando em  http://%s:%d" % (
        "127.0.0.1" if args.host == "0.0.0.0" else args.host, args.port))
    if args.host == "0.0.0.0":
        print("  App Android no emulador: BASE_URL = http://10.0.2.2:%d/" % args.port)
    print("  Arquivos gerados em: %s\n" % PASTA_SAIDAS)
    app.run(host=args.host, port=args.port, debug=False)