"""
Context-feature nonconformity for PCCF (phase 2), additive and research-only.

WHY: the phase-1 feasibility check (PCCF_FEASIBILITY_RESULTS.md) found that
the presence mask is highly informative, but the layers' own confidence
barely separates real names from false hits inside a pattern (the
dictionary is binary, Presidio reports a constant 0.85, and beam AUROC is
about 0.6). The recoverable recall is in the single-layer patterns. This
module builds the within-pattern signal from the failure modes that were
already root-caused:
  - place-name collision (dictionary layer)
  - clinical-header and disease-eponym confusion (NER layer)
It uses only the text around a candidate. No new model is loaded. The
scorer is a small L2 logistic regression (numpy only) or a hand-written
rule, and its output becomes the candidate's nonconformity r = 1 - p.
src/pccf.py is used unchanged.

CUE LISTS ARE PER LANGUAGE (see ES_CUES). They were fixed by inspecting
the MEDDOCAN *train* split only, before any validation or test document
was scored. A different language needs its own cue list; that dependency
is the price of this signal and should be stated as one.

FIELD LABELS: MEDDOCAN documents open with a form-style header
("Nombre: ...", "Localidad/ Provincia: ..."). That is structurally the
same as key=value telemetry, where the field name is the analogous cue
(the idea behind detect.py's field-gated paths). Because it can also be a
template artifact of this corpus, the harness runs an ablation without
these features and reports header and narrative text separately.
"""
from __future__ import annotations

import re

import numpy as np

_WORD = r"[\wáéíóúñüÁÉÍÓÚÑÜ\.ª]+"

ES_CUES = {
    "name_labels": {"nombre", "apellidos", "médico", "medico", "responsable clínico",
                    "remitido por", "nombre y apellidos"},
    "place_labels": {"localidad/ provincia", "localidad", "provincia", "país",
                     "país de nacimiento", "domicilio", "cp", "dirección"},
    "honorifics": {"dr.", "dra.", "dr", "dra", "d.", "dña.", "sr.", "sra."},
    "place_preps": {"en", "de", "del", "desde", "hacia", "a"},
    "eponym_heads": ("síndrome de", "sindrome de", "enfermedad de", "signo de",
                     "maniobra de", "test de", "prueba de", "células de", "tumor de"),
}

FEATURE_NAMES = [
    "bias", "pat_dict", "pat_ner", "pat_both",
    "label_name", "label_place", "label_other", "label_none",
    "honorific_before", "prev_place_prep", "eponym_before",
    "followed_by_colon", "line_initial", "n_words", "has_lower_word",
    "has_digit", "short_caps_token", "ner_conf", "has_ner_conf",
]
FIELD_LABEL_FEATURES = {"label_name", "label_place", "label_other", "label_none"}


def field_label(text: str, start: int, cues=ES_CUES):
    """(category, raw_label) for the 'Label:' prefix on the candidate's line.
    category is one of name / place / other / none."""
    line_start = text.rfind("\n", 0, start) + 1
    before = text[line_start:start]
    m = re.match(r"\s*([^:\n]{1,40}):", before)
    if not m:
        return "none", ""
    lab = m.group(1).replace("﻿", "").strip().lower()
    if lab in cues["name_labels"]:
        return "name", lab
    if lab in cues["place_labels"]:
        return "place", lab
    return "other", lab


def features(text: str, cand, cues=ES_CUES) -> dict:
    s, e = cand.start, cand.end
    span = text[s:e]
    before = text[max(0, s - 40):s]
    prev_words = re.findall(_WORD, before)
    prev = prev_words[-1].lower() if prev_words else ""
    line_start = text.rfind("\n", 0, s) + 1
    cat, _ = field_label(text, s, cues)
    words = span.split()
    ner_conf = 1.0 - cand.scores["ner"] if "ner" in cand.scores else 0.0
    after = text[e:e + 3]
    mask = cand.mask
    return {
        "bias": 1.0,
        "pat_dict": float(mask == frozenset({"dict"})),
        "pat_ner": float(mask == frozenset({"ner"})),
        "pat_both": float(mask == frozenset({"dict", "ner"})),
        "label_name": float(cat == "name"),
        "label_place": float(cat == "place"),
        "label_other": float(cat == "other"),
        "label_none": float(cat == "none"),
        "honorific_before": float(prev in cues["honorifics"]),
        "prev_place_prep": float(prev in cues["place_preps"]),
        "eponym_before": float(before.lower().rstrip().endswith(cues["eponym_heads"])),
        "followed_by_colon": float(after.lstrip().startswith(":")),
        "line_initial": float(text[line_start:s].strip() == ""),
        "n_words": float(min(len(words), 5)) / 5.0,
        "has_lower_word": float(any(w[:1].islower() for w in words)),
        "has_digit": float(any(ch.isdigit() for ch in span)),
        "short_caps_token": float(any(len(w) <= 4 and sum(c.isupper() for c in w) >= 2 for w in words)),
        "ner_conf": ner_conf,
        "has_ner_conf": float("ner" in cand.scores),
    }


def to_matrix(feats: list, drop=(), cols=None):
    if cols is None:
        names = list(feats[0].keys()) if feats else FEATURE_NAMES
        cols = [f for f in names if f not in drop]
    return np.array([[fd[c] for c in cols] for fd in feats], dtype=float).reshape(len(feats), len(cols)), cols


class LogisticScorer:
    """L2-regularized logistic regression by Newton/IRLS (numpy only)."""

    def __init__(self, l2: float = 1.0, drop=()):
        self.l2 = l2
        self.drop = tuple(drop)
        self.w = None
        self.cols = None

    def fit(self, feats, labels, iters: int = 50):
        X, self.cols = to_matrix(feats, self.drop)
        y = np.asarray(labels, dtype=float)
        w = np.zeros(X.shape[1])
        reg = self.l2 * np.eye(X.shape[1])
        reg[0, 0] = 0.0  # no penalty on bias
        for _ in range(iters):
            p = 1 / (1 + np.exp(-X @ w))
            g = X.T @ (p - y) + reg @ w
            H = X.T @ (X * (p * (1 - p))[:, None]) + reg
            step = np.linalg.solve(H, g)
            w -= step
            if np.abs(step).max() < 1e-8:
                break
        self.w = w
        return self

    def predict(self, feats):
        X, _ = to_matrix(feats, self.drop, self.cols)
        return 1 / (1 + np.exp(-X @ self.w))

    def weights(self):
        return dict(zip(self.cols, map(float, self.w)))


class RuleScorer:
    """Hand-written, fit-free scorer (the auditable ablation). Starts from
    the phase-1 pattern precision and moves it with the root-caused cues."""
    BASE = {"pat_dict": 0.15, "pat_ner": 0.10, "pat_both": 0.94}

    def predict(self, feats):
        out = []
        for f in feats:
            p = sum(v for k, v in self.BASE.items() if f[k])
            if f["label_name"] or f["honorific_before"]:
                p = max(p, 0.97)
            if f["label_place"] or f["eponym_before"] or f["followed_by_colon"]:
                p = min(p, 0.03)
            if f["has_digit"] or f["short_caps_token"] or f["has_lower_word"]:
                p = min(p, 0.05)
            out.append(p)
        return np.array(out)


def apply_scores(cands, probs):
    """Write r = 1 - p onto every present layer, so that PCCF's joint max
    equals 1 - p."""
    for c, p in zip(cands, probs):
        c.scores = {k: float(1.0 - p) for k in c.scores}
    return cands


# ==========================================================================
# Phase 3 additions (PCCF_PHASE3_PREREGISTRATION.md)
# ==========================================================================
# French / Russian cue lists: written from language knowledge BEFORE any
# FR/RU candidate was inspected (T4). Same keys as ES_CUES, so features()
# and RuleScorer work unchanged.
FR_CUES = {
    "name_labels": {"nom", "prénom", "prenom", "nom complet", "nom et prénom", "patient", "client"},
    "place_labels": {"adresse", "ville", "lieu de naissance", "pays", "code postal", "domicile"},
    "honorifics": {"m.", "mme", "mme.", "mlle", "monsieur", "madame", "mademoiselle", "dr", "dr.",
                   "docteur", "me", "pr", "pr."},
    "place_preps": {"à", "en", "de", "du", "des", "au", "aux", "vers", "depuis"},
    "eponym_heads": ("syndrome de", "maladie de", "signe de", "test de"),
}
RU_CUES = {
    "name_labels": {"фио", "имя", "фамилия", "отчество", "клиент", "пациент", "получатель", "заявитель"},
    "place_labels": {"адрес", "город", "место рождения", "страна", "индекс", "регион"},
    "honorifics": {"уважаемый", "уважаемая", "господин", "госпожа", "г-н", "г-жа", "доктор", "д-р"},
    "place_preps": {"в", "во", "из", "на", "до", "под", "над"},
    "eponym_heads": ("синдром", "болезнь", "болезни", "синдрома"),
}

# --------------------------------------------------------------------------
# Key/value telemetry features (T3). In a log line the analogue of MEDDOCAN's
# "Nombre:" label is the field key (or syslog phrase) right before the value.
# --------------------------------------------------------------------------
TELEMETRY_IDENTITY_KEYS = {
    "user", "username", "user_name", "targetusername", "subjectusername",
    "accountname", "logname", "ruser", "useridentity.username", "for user",
    "invalid user", "samaccountname", "principal", "owner", "login",
}
_KV_KEY_RE = re.compile(r'"?([A-Za-z_][\w\.]*)"?\s*[=:]\s*"?$')
_PHRASE_RE = re.compile(r"\b(invalid user|for user|user|by user|logname|ruser)\s+$", re.IGNORECASE)

KV_FEATURE_NAMES = [
    "bias", "pat_flat", "pat_ner", "pat_both", "key_identity", "key_other", "key_none",
    "n_words", "has_digit", "has_dot_or_at", "has_underscore", "all_lower",
    "title_case_words", "ner_conf", "has_ner_conf",
]


def kv_key(text: str, start: int):
    before = text[max(0, start - 60):start]
    m = _PHRASE_RE.search(before)
    if m:
        return m.group(1).lower()
    m = _KV_KEY_RE.search(before)
    if m:
        return m.group(1).lower()
    return None


def kv_features(text: str, cand) -> dict:
    span = text[cand.start:cand.end]
    words = span.split()
    key = kv_key(text, cand.start)
    ident = key is not None and (key in TELEMETRY_IDENTITY_KEYS or key.split(".")[-1] in TELEMETRY_IDENTITY_KEYS)
    mask = cand.mask
    return {
        "bias": 1.0,
        "pat_flat": float(mask == frozenset({"flat"})),
        "pat_ner": float(mask == frozenset({"ner"})),
        "pat_both": float(mask == frozenset({"flat", "ner"})),
        "key_identity": float(ident),
        "key_other": float(key is not None and not ident),
        "key_none": float(key is None),
        "n_words": float(min(len(words), 5)) / 5.0,
        "has_digit": float(any(ch.isdigit() for ch in span)),
        "has_dot_or_at": float("." in span or "@" in span),
        "has_underscore": float("_" in span),
        "all_lower": float(span.islower()),
        "title_case_words": float(bool(words) and all(w[:1].isupper() for w in words)),
        "ner_conf": 1.0 - cand.scores["ner"] if "ner" in cand.scores else 0.0,
        "has_ner_conf": float("ner" in cand.scores),
    }


class KVRuleScorer:
    """Fit-free telemetry rule: identity key -> name-like, other key ->
    not, no key -> neutral (ranked by NER confidence when present)."""

    def predict(self, feats):
        out = []
        for f in feats:
            if f["key_identity"]:
                p = 0.97
            elif f["key_other"]:
                p = 0.03
            else:
                p = 0.5 * (0.5 + 0.5 * f["ner_conf"]) if f["has_ner_conf"] else 0.25
            out.append(p)
        return np.array(out)
