#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SGE - Equipe 5: Lista de Chamada -> Certificado -> Crachá   (Python + Flask)

Simula o MESMO app Android (as 3 telas, o mesmo fluxo e as mesmas regras) num único arquivo.
Também serve como API fake: o app Android consegue chamar essas rotas de verdade.

Como rodar
    pip install flask pillow
    python sge_app.py                      -> http://127.0.0.1:5000
    python sge_app.py --host 0.0.0.0       -> aceita conexões da rede (celular / emulador)

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

from flask import Flask, jsonify, render_template_string, request, send_file
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
#  TELAS (HTML/CSS/JS). Paleta: branco (principal) e laranja (secundária).
# =====================================================================================
CSS = r"""
:root{--laranja:#EF6C00;--branco:#fff;--texto:#212121;--texto2:#757575;--linha:#E0E0E0;--claro:#FFF3E0}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;height:100%}
body{background:#ececec;font-family:Roboto,"Segoe UI",system-ui,sans-serif;color:var(--texto)}
.app{display:flex;flex-direction:column;width:100%;max-width:420px;height:100vh;margin:0 auto;background:var(--branco);position:relative;overflow:hidden}
@media(min-width:480px){body{padding:20px 0}.app{height:calc(100vh - 40px);max-height:880px;border-radius:28px;border:2px solid #c9c9c9}}
.topo{position:relative;display:flex;flex-direction:column;align-items:center;padding:16px 20px 12px}
.topo-img{width:100%;max-height:120px;object-fit:contain}
.topo h1{margin:10px 0 0;font-size:22px;font-weight:700}
.voltar{position:absolute;left:8px;top:8px;width:40px;height:40px;border:0;background:none;color:var(--laranja);font-size:34px;line-height:1;cursor:pointer}
.faixa{height:3px;background:var(--laranja);flex:none}
.resumo{padding:12px 20px 2px;font-size:14px;color:var(--texto2)}
.dica{padding:0 20px 8px;font-size:12px;color:var(--laranja)}
.prog{height:3px;background:#FFE0B2;overflow:hidden;visibility:hidden;flex:none}
.prog.on{visibility:visible}
.prog i{display:block;height:100%;width:35%;background:var(--laranja);animation:mv 1s infinite linear}
@keyframes mv{from{margin-left:-35%}to{margin-left:100%}}
.lista{flex:1;overflow-y:auto;padding:0 8px}
.linha{display:flex;align-items:center;gap:8px;min-height:60px;padding:10px 12px;border-bottom:.5px solid var(--linha);cursor:pointer}
.linha:active{background:var(--claro)}
.nome{flex:1;font-size:16px}
.status{font-size:14px;font-weight:700;min-width:62px;text-align:right;margin-right:6px}
.status.ok{color:var(--laranja)}.status.no{color:var(--texto2)}
.sw{position:relative;width:40px;height:22px;flex:none}
.sw input{position:absolute;inset:0;opacity:0;width:100%;height:100%;margin:0;cursor:pointer}
.trilho{position:absolute;inset:0;border-radius:11px;background:#E0E0E0;transition:background .15s;pointer-events:none}
.trilho::after{content:"";position:absolute;top:3px;left:3px;width:16px;height:16px;border-radius:50%;background:#9E9E9E;transition:left .15s,background .15s}
.sw input:checked+.trilho{background:var(--laranja)}
.sw input:checked+.trilho::after{left:21px;background:#fff}
.rodape{flex:none;border-top:.5px solid var(--linha);padding:14px 32px max(18px,env(safe-area-inset-bottom));background:var(--branco)}
.btn-grande,.btn-contorno{display:block;width:100%;border-radius:32px;font:inherit;font-weight:700;cursor:pointer}
.btn-grande{height:64px;border:0;background:var(--laranja);color:#fff;font-size:18px}
.btn-grande:disabled{opacity:.5;cursor:default}
.btn-contorno{height:52px;margin-top:10px;border:2px solid var(--laranja);background:#fff;color:var(--laranja);font-size:16px}
.btn-contorno[hidden]{display:none}
.form{flex:1;overflow-y:auto;padding:4px 20px 20px}
.form label{display:block;margin-top:16px;font-size:13px;color:var(--texto2)}
.form input,.form select{width:100%;height:48px;border:0;border-bottom:2px solid var(--laranja);border-radius:0;background:transparent;font:inherit;font-size:16px;color:var(--texto);outline:none}
.form input::placeholder{color:#9E9E9E}
.previa{margin-top:24px;height:180px;padding:8px;background:var(--claro);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;text-align:center}
.previa img{width:100px;height:65px;object-fit:contain}
.previa .ev{font-size:14px}.previa .pa{font-size:16px;font-weight:700}
.resultado{margin-top:20px;padding:12px 14px;border:1px solid var(--laranja);border-radius:12px;background:var(--claro);font-size:14px}
.resultado[hidden]{display:none}
.resultado a{color:var(--laranja);font-weight:700}
.resultado img{display:block;width:60%;margin:10px auto 0;border:1px solid var(--linha)}
.toast{position:fixed;left:50%;bottom:200px;transform:translateX(-50%);max-width:340px;background:#212121;color:#fff;font-size:13px;padding:9px 16px;border-radius:18px;opacity:0;transition:opacity .2s;pointer-events:none;text-align:center;z-index:9}
.toast.on{opacity:1}
"""

BASE_HTML = r"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>{{ titulo }}</title><style>{{ css|safe }}</style></head>
<body><div class="app">{{ corpo|safe }}</div><div id="toast" class="toast"></div>
<script>{{ js_comum|safe }}</script><script>{{ script|safe }}</script></body></html>"""

JS_COMUM = r"""
const $ = (id) => document.getElementById(id);
const qs = new URLSearchParams(location.search);
let _t;
function toast(msg){const t=$('toast');t.textContent=msg;t.classList.add('on');clearTimeout(_t);_t=setTimeout(()=>t.classList.remove('on'),2600);}
function irPara(caminho,dados){location.href=caminho+'?'+new URLSearchParams(dados).toString();}
async function api(url,opcoes){const r=await fetch(url,opcoes);let corpo=null;try{corpo=await r.json();}catch(e){}return{ok:r.ok,status:r.status,corpo:corpo};}
const JSON_H={'Content-Type':'application/json'};
"""

# ------------------------------- Tela 1: lista de presença -------------------------------
CORPO_PRESENCA = r"""
<header class="topo"><img class="topo-img" src="/logo" alt="Logo"><h1>Lista de Chamada</h1></header>
<div class="faixa"></div>
<div class="resumo" id="resumo">Presentes: 0 | Faltas: 0 | Total: 0</div>
<div class="dica">Toque num participante presente para emitir o certificado</div>
<div class="prog" id="prog"><i></i></div>
<main class="lista" id="lista"></main>
<footer class="rodape"><button class="btn-grande" id="btnSalvar">Salvar Presenças</button></footer>
"""

JS_PRESENCA = r"""
const idEvento = parseInt(qs.get('evento_id') || '1', 10);
const nomeEvento = qs.get('evento') || 'Evento X';
let lista = [];

function resumo(){
  const p = lista.filter(c => c.presente).length, t = lista.length;
  $('resumo').textContent = `Presentes: ${p} | Faltas: ${t - p} | Total: ${t}`;
}
function carregando(on){
  $('prog').classList.toggle('on', on);
  $('btnSalvar').disabled = on;
}
function render(){
  const box = $('lista'); box.innerHTML = '';
  lista.forEach(c => {
    const row = document.createElement('div'); row.className = 'linha';
    const nome = document.createElement('span'); nome.className = 'nome'; nome.textContent = c.nome;
    const st = document.createElement('span');
    const sw = document.createElement('label'); sw.className = 'sw';
    const input = document.createElement('input'); input.type = 'checkbox';
    input.setAttribute('aria-label', 'Presença de ' + c.nome);
    const trilho = document.createElement('span'); trilho.className = 'trilho';
    sw.append(input, trilho);
    const pintar = () => { st.textContent = c.presente ? 'Presente' : 'Falta'; st.className = 'status ' + (c.presente ? 'ok' : 'no'); input.checked = c.presente; };
    input.addEventListener('change', () => { c.presente = input.checked; c.alterado = true; pintar(); resumo(); });
    sw.addEventListener('click', e => e.stopPropagation());
    row.addEventListener('click', () => abrirCertificado(c));
    pintar();
    row.append(nome, st, sw); box.appendChild(row);
  });
}
async function carregar(){
  carregando(true);
  const r = await api(`/eventos/${idEvento}/chamada`).catch(() => ({ok:false}));
  carregando(false);
  if (!r.ok) { toast('Erro ao carregar a lista'); return; }
  lista = r.corpo.map(c => ({id:c.id, idParticipante:c.id_participante, nome:c.nome_participante, presente:c.presente, alterado:false}));
  render(); resumo();
}
async function salvar(){
  const alteradas = lista.filter(c => c.alterado);
  if (!alteradas.length) { toast('Nenhuma alteração pra salvar'); return; }
  carregando(true);
  let erros = 0;
  await Promise.all(alteradas.map(async c => {
    const r = await api(`/chamada/${c.id}/presenca`, {method:'PUT', headers:JSON_H, body:JSON.stringify({presente:c.presente})}).catch(() => ({ok:false}));
    if (r.ok) c.alterado = false; else erros++;
  }));
  carregando(false);
  toast(erros === 0 ? 'Presenças salvas!' : `${erros} falha(s) ao salvar. Toque em Salvar de novo.`);
}
function abrirCertificado(c){
  if (!c.presente) { toast('Certificado só pra quem tem presença validada'); return; }
  if (c.alterado)  { toast('Salve as presenças antes de emitir o certificado'); return; }
  irPara('/certificado', {evento_id:idEvento, evento:nomeEvento, participante_id:c.idParticipante, participante:c.nome});
}
$('btnSalvar').addEventListener('click', salvar);
carregar();
"""

# ------------------------------- Tela 2: certificado -------------------------------
CORPO_CERTIFICADO = r"""
<header class="topo"><button class="voltar" onclick="history.back()" aria-label="Voltar">&#8249;</button>
<img class="topo-img" src="/logo" alt="Logo"><h1>Emitir Certificado</h1></header>
<div class="faixa"></div>
<main class="form">
  <label for="campoParticipante">Participante</label>
  <input id="campoParticipante" placeholder="Nome do participante" autocomplete="off">
  <label for="campoEvento">Evento</label>
  <input id="campoEvento" placeholder="Nome do evento" autocomplete="off">
  <label for="spnTipo">Tipo</label><select id="spnTipo"></select>
  <label for="spnModelo">Modelo</label><select id="spnModelo"></select>
  <div class="resultado" id="resultado" hidden></div>
</main>
<footer class="rodape">
  <button class="btn-grande" id="btnGerar">Gerar Certificado</button>
  <button class="btn-contorno" id="btnCracha" hidden>Continuar para o crachá</button>
</footer>
"""

JS_CERTIFICADO = r"""
const idEvento = parseInt(qs.get('evento_id') || '0', 10);
const idParticipante = parseInt(qs.get('participante_id') || '0', 10);
$('campoParticipante').value = qs.get('participante') || '';
$('campoEvento').value = qs.get('evento') || '';

function preencher(select, itens){ select.innerHTML = ''; itens.forEach(t => { const o = document.createElement('option'); o.textContent = t; select.appendChild(o); }); }
async function carregarOpcoes(){
  const r = await api('/api/opcoes');
  if (!r.ok) return;
  preencher($('spnTipo'), r.corpo.tipos);
  preencher($('spnModelo'), r.corpo.modelos);
  if (idEvento) {  // modelo escolhido em cascata: evento -> curso -> padrão
    const m = await api('/modelos/resolver/' + idEvento);
    if (m.ok) $('spnModelo').value = m.corpo.nome;
  }
}
$('btnGerar').addEventListener('click', async () => {
  const participante = $('campoParticipante').value.trim(), evento = $('campoEvento').value.trim();
  if (!participante || !evento) { toast('Preencha o participante e o evento.'); return; }
  $('btnGerar').disabled = true;
  const r = await api('/certificados', {method:'POST', headers:JSON_H, body:JSON.stringify({
    participante, evento, tipo:$('spnTipo').value, modelo:$('spnModelo').value,
    participante_id: idParticipante || null, evento_id: idEvento || null})}).catch(() => ({ok:false}));
  $('btnGerar').disabled = false;
  if (!r.ok) { toast((r.corpo && r.corpo.erro) || 'Erro ao gerar PDF'); return; }
  const box = $('resultado'); box.hidden = false;
  box.innerHTML = 'PDF salvo em <b></b><br><a target="_blank" href="' + r.corpo.url + '">Abrir PDF</a>';
  box.querySelector('b').textContent = r.corpo.arquivo;
  $('btnCracha').hidden = false;
  toast('PDF salvo em ' + r.corpo.arquivo);
});
$('btnCracha').addEventListener('click', () => irPara('/cracha', {
  evento_id: idEvento, evento: $('campoEvento').value.trim(),
  participante_id: idParticipante, participante: $('campoParticipante').value.trim()}));
carregarOpcoes();
"""

# ------------------------------- Tela 3: crachá -------------------------------
CORPO_CRACHA = r"""
<header class="topo"><button class="voltar" onclick="history.back()" aria-label="Voltar">&#8249;</button>
<h1>Gerador de Crachá</h1></header>
<div class="faixa"></div>
<main class="form">
  <label for="campoParticipante">Participante</label>
  <input id="campoParticipante" placeholder="Nome do participante" autocomplete="off">
  <label for="campoEvento">Evento</label>
  <input id="campoEvento" placeholder="Nome do evento" autocomplete="off">
  <div class="previa" id="previaCracha">
    <img src="/logo" alt="Logo do evento">
    <div class="ev" id="txtEvento">NOME DO EVENTO</div>
    <div class="pa" id="txtParticipante">NOME DO PARTICIPANTE</div>
  </div>
  <div class="resultado" id="resultado" hidden></div>
</main>
<footer class="rodape"><button class="btn-grande" id="botaoGerar">Gerar imagem</button></footer>
"""

JS_CRACHA = r"""
$('campoParticipante').value = qs.get('participante') || '';
$('campoEvento').value = qs.get('evento') || '';
$('botaoGerar').addEventListener('click', async () => {
  const participante = $('campoParticipante').value.trim(), evento = $('campoEvento').value.trim();
  if (!participante || !evento) { toast('Preencha o evento e o participante!'); return; }
  $('txtParticipante').textContent = participante.toUpperCase();
  $('txtEvento').textContent = evento;
  $('botaoGerar').disabled = true;
  const r = await api('/crachas', {method:'POST', headers:JSON_H, body:JSON.stringify({participante, evento})}).catch(() => ({ok:false}));
  $('botaoGerar').disabled = false;
  if (!r.ok) { toast((r.corpo && r.corpo.erro) || 'Erro ao salvar a imagem!'); return; }
  const box = $('resultado'); box.hidden = false;
  box.innerHTML = 'Imagem salva em: <b></b><br><a target="_blank" href="' + r.corpo.url + '">Abrir imagem</a><img alt="Crachá gerado" src="' + r.corpo.url + '">';
  box.querySelector('b').textContent = r.corpo.arquivo;
  toast('Imagem salva em:\n' + r.corpo.arquivo);
});
"""


def pagina(titulo, corpo, script):
    return render_template_string(BASE_HTML, titulo=titulo, css=CSS, corpo=corpo,
                                  js_comum=JS_COMUM, script=script)


@app.get("/")
def tela_presenca():
    return pagina("Lista de Chamada", CORPO_PRESENCA, JS_PRESENCA)


@app.get("/certificado")
def tela_certificado():
    return pagina("Emitir Certificado", CORPO_CERTIFICADO, JS_CERTIFICADO)


@app.get("/cracha")
def tela_cracha():
    return pagina("Gerador de Crachá", CORPO_CRACHA, JS_CRACHA)


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