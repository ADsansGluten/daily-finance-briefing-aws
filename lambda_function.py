"""Briefing finance quotidien - AWS Lambda (Python 3.12).

Flux : flux RSS -> sélection/déduplication -> résumé par Amazon Bedrock (Converse API)
        -> page HTML dans S3 -> e-mail via Amazon SES.

Aucune dépendance externe : boto3 est déjà fourni par Lambda, le reste est la
bibliothèque standard. On peut donc coller ce fichier directement dans l'éditeur
de la console Lambda.

Variables d'environnement
  BUCKET      (obligatoire) nom du bucket S3
  SENDER      (obligatoire) adresse expéditrice, vérifiée dans SES
  RECIPIENT   (obligatoire) adresse destinataire (vérifiée aussi tant que SES est en sandbox)
  MODEL_ID    (optionnel)   modèle ou profil d'inférence Bedrock, défaut : eu.amazon.nova-2-lite-v1:0
                          (à vérifier dans Bedrock > Profils d'inférence)
  FEEDS       (optionnel)   "Nom|url;Nom|url;..." pour remplacer les flux par défaut
  MAX_ITEMS   (optionnel)   nombre d'articles envoyés au modèle, défaut 24
  MAX_AGE_H   (optionnel)   ancienneté maximale des articles en heures, défaut 30

Événements de test :
  {"dry_run": true}  -> tout est exécuté sauf l'envoi de l'e-mail
  {"no_ai": true}    -> n'appelle pas Bedrock (résumé simplifié à partir des titres), utile tant que
                        le compte AWS est en cours de vérification ou pour tester les flux RSS
"""
import html
import json
import logging
import math
import os
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DEFAULT_FEEDS = [
    ("Le Monde Économie", "https://www.lemonde.fr/economie/rss_full.xml"),
    ("BBC Business", "https://feeds.bbci.co.uk/news/business/rss.xml"),
    ("CNBC Finance", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664"),
    ("BCE", "https://www.ecb.europa.eu/rss/press.html"),
]

MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
        "août", "septembre", "octobre", "novembre", "décembre"]
JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]

SYSTEM_PROMPT = (
    "Tu es un analyste financier qui rédige un briefing quotidien en français pour un "
    "étudiant en data et IA qui s'intéresse aux marchés. Règles : utilise UNIQUEMENT les "
    "articles fournis, n'invente aucun chiffre ni aucun fait, cite les sources par leur "
    "numéro entre crochets, par exemple [3], reste factuel et neutre, et ne donne aucun "
    "conseil d'investissement. Si les articles ne permettent pas de remplir une section, "
    "écris « Rien de notable. »"
)

PROMPT_TEMPLATE = """Nous sommes le {date}. Voici les articles des dernières 24 à 30 heures :

{articles}

Rédige le briefing exactement avec cette structure (titres précédés de ## et puces commençant par "- ") :

## À retenir
(3 à 5 puces, les faits les plus importants)
## Marchés et entreprises
(2 à 5 puces)
## Macro et banques centrales
(1 à 4 puces)
## À surveiller aujourd'hui
(1 à 3 puces, uniquement si les articles mentionnent un événement à venir)

Chaque puce doit citer au moins une source [n]. Maximum 220 mots au total."""

bedrock = boto3.client("bedrock-runtime")
s3 = boto3.client("s3")
ses = boto3.client("sesv2")


# --------------------------------------------------------------------------- flux RSS
def _parse_date(text):
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _clean(text, limit=260):
    text = re.sub(r"<[^>]+>", " ", html.unescape(text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _text(element, tag):
    found = element.find(tag)
    return (found.text or "").strip() if found is not None else ""


def fetch_feed(source, url, timeout=10):
    """Lit un flux RSS 2.0 ou Atom et renvoie une liste d'articles normalisés."""
    request = urllib.request.Request(url, headers={"User-Agent": "DailyFinanceBriefing/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        root = ET.fromstring(response.read())  # flux de sources de confiance uniquement

    items = []
    for node in root.iter("item"):  # RSS
        items.append({
            "source": source,
            "title": _clean(_text(node, "title"), 200),
            "summary": _clean(_text(node, "description")),
            "link": _text(node, "link"),
            "date": _parse_date(_text(node, "pubDate")),
        })
    atom = "{http://www.w3.org/2005/Atom}"
    for node in root.iter(atom + "entry"):  # Atom
        link_node = node.find(atom + "link")
        items.append({
            "source": source,
            "title": _clean(_text(node, atom + "title"), 200),
            "summary": _clean(_text(node, atom + "summary")),
            "link": link_node.get("href", "") if link_node is not None else "",
            "date": _parse_date(_text(node, atom + "updated") or _text(node, atom + "published")),
        })
    return items


def collect(feeds, max_age_h, max_items):
    """Récupère, filtre par âge, déduplique et équilibre les sources."""
    oldest = datetime.now(timezone.utc) - timedelta(hours=max_age_h)
    per_source = max(1, math.ceil(max_items / max(1, len(feeds))))
    far_past = datetime.min.replace(tzinfo=timezone.utc)
    seen, selected = set(), []

    for source, url in feeds:
        try:
            fetched = fetch_feed(source, url)
        except Exception as exc:  # un flux en panne ne doit pas bloquer le briefing
            logger.warning("Flux ignoré (%s) : %s", source, exc)
            continue
        fetched = [i for i in fetched if i["title"] and (i["date"] is None or i["date"] >= oldest)]
        fetched.sort(key=lambda i: i["date"] or far_past, reverse=True)
        kept = 0
        for item in fetched:
            key = re.sub(r"\W+", "", item["title"].lower())
            if key in seen:
                continue
            seen.add(key)
            selected.append(item)
            kept += 1
            if kept >= per_source:
                break
        logger.info("%s : %d article(s) retenu(s)", source, kept)

    selected.sort(key=lambda i: i["date"] or far_past, reverse=True)
    return selected[:max_items]


# --------------------------------------------------------------------------- Bedrock
def summarize(items, date_label, model_id):
    articles = "\n".join(
        f"[{n}] ({i['source']}) {i['title']} - {i['summary']}" for n, i in enumerate(items, 1)
    )
    response = bedrock.converse(
        modelId=model_id,
        system=[{"text": SYSTEM_PROMPT}],
        messages=[{"role": "user", "content": [{"text": PROMPT_TEMPLATE.format(date=date_label, articles=articles)}]}],
        inferenceConfig={"maxTokens": 2500, "temperature": 0.2},  # marge : Nova 2 Lite peut dépenser des tokens en raisonnement
    )
    if response.get("stopReason") == "max_tokens":
        logger.warning("Réponse tronquée (max_tokens) : augmenter maxTokens.")
    blocks = response["output"]["message"]["content"]
    text = "".join(block["text"] for block in blocks if "text" in block).strip()
    return text, response.get("usage", {})


def fallback_summary(items):
    """Résumé minimal sans IA : les titres les plus récents, avec leur numéro de source."""
    bullets = "\n".join(f"- {i['title']} [{n}]" for n, i in enumerate(items[:6], 1))
    return f"## À retenir\n{bullets}\n## Marchés et entreprises\n- Résumé IA non généré (mode sans IA)."


# --------------------------------------------------------------------------- rendu
def render_body(summary_text, items):
    parts, in_list = [], False
    for raw in summary_text.replace("**", "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            if in_list:
                parts.append("</ul>")
                in_list = False
            parts.append(f"<h3 style='margin:22px 0 6px;color:#4c1d95'>{html.escape(line.lstrip('# ').strip())}</h3>")
        elif line[:2] in ("- ", "* ") or line.startswith("• "):
            if not in_list:
                parts.append("<ul style='padding-left:20px;margin:0'>")
                in_list = True
            parts.append(f"<li style='margin:6px 0'>{html.escape(line[2:].strip())}</li>")
        else:
            if in_list:
                parts.append("</ul>")
                in_list = False
            parts.append(f"<p>{html.escape(line)}</p>")
    if in_list:
        parts.append("</ul>")

    sources = "".join(
        f"<li style='margin:3px 0'>[{n}] {html.escape(i['source'])} - "
        f"<a href='{html.escape(i['link'], quote=True)}'>{html.escape(i['title'])}</a></li>"
        for n, i in enumerate(items, 1) if i["link"]
    )
    parts.append("<h3 style='margin:26px 0 6px;color:#4c1d95'>Sources</h3>"
                 f"<ol style='padding-left:0;list-style:none;font-size:13px;color:#555'>{sources}</ol>")
    return "".join(parts)


def render_page(title, body):
    return (
        "<!doctype html><html lang='fr'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title></head>"
        "<body style=\"font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:680px;"
        "margin:32px auto;padding:0 16px;line-height:1.5;color:#1f2937\">"
        f"<h1 style='font-size:22px;margin-bottom:0'>{html.escape(title)}</h1>"
        f"{body}"
        "<p style='margin-top:30px;font-size:12px;color:#888'>Résumé généré par IA à partir de flux "
        "publics. Ne constitue pas un conseil en investissement.</p></body></html>"
    )


# --------------------------------------------------------------------------- handler
def _load_feeds():
    raw = os.environ.get("FEEDS", "").strip()
    if not raw:
        return DEFAULT_FEEDS
    feeds = []
    for chunk in raw.split(";"):
        if "|" in chunk:
            name, url = chunk.split("|", 1)
            feeds.append((name.strip(), url.strip()))
    return feeds or DEFAULT_FEEDS


def lambda_handler(event, context):
    event = event or {}
    dry_run = bool(event.get("dry_run", False))
    no_ai = bool(event.get("no_ai", False))

    bucket = os.environ["BUCKET"]
    model_id = os.environ.get("MODEL_ID", "eu.amazon.nova-2-lite-v1:0")
    max_items = int(os.environ.get("MAX_ITEMS", "24"))
    max_age_h = int(os.environ.get("MAX_AGE_H", "30"))

    now = datetime.now(ZoneInfo("Europe/Paris"))
    date_label = f"{JOURS[now.weekday()]} {now.day} {MOIS[now.month - 1]} {now.year}"
    iso_day = now.strftime("%Y-%m-%d")

    items = collect(_load_feeds(), max_age_h, max_items)
    if not items:
        logger.error("Aucun article récupéré : vérifier les flux dans les logs.")
        return {"status": "no_articles"}

    if no_ai:
        summary, usage = fallback_summary(items), {}
    else:
        summary, usage = summarize(items, date_label, model_id)
    title = f"Briefing finance - {date_label}"
    page = render_page(title, render_body(summary, items))

    for key in (f"briefings/{iso_day}.html", "briefings/latest.html"):
        s3.put_object(Bucket=bucket, Key=key, Body=page.encode("utf-8"),
                      ContentType="text/html; charset=utf-8")
    s3.put_object(
        Bucket=bucket, Key=f"briefings/{iso_day}.json", ContentType="application/json",
        Body=json.dumps({"date": iso_day, "model": model_id, "summary": summary,
                         "items": [{**i, "date": i["date"].isoformat() if i["date"] else None} for i in items]},
                        ensure_ascii=False).encode("utf-8"),
    )

    if not dry_run:
        ses.send_email(
            FromEmailAddress=os.environ["SENDER"],
            Destination={"ToAddresses": [os.environ["RECIPIENT"]]},
            Content={"Simple": {
                "Subject": {"Data": title, "Charset": "UTF-8"},
                "Body": {"Html": {"Data": page, "Charset": "UTF-8"},
                         "Text": {"Data": summary, "Charset": "UTF-8"}},
            }},
        )

    result = {"status": "ok", "dry_run": dry_run, "no_ai": no_ai, "articles": len(items),
              "input_tokens": usage.get("inputTokens"), "output_tokens": usage.get("outputTokens")}
    logger.info(json.dumps(result))
    return result
