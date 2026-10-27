"""
Presidio + NER layer (second detection pass after phi_scrub's regex rules). Fully local.

NER engines (env AR_AUDIT_NER):
  spacy  (default)  spaCy model from AR_AUDIT_SPACY (default en_core_web_sm; en_core_web_lg is more accurate)
  hf                transformer de-identification NER (AR_AUDIT_HF_MODEL, default StanfordAIMI/stanford-deidentifier-base)
                    run through Presidio's TransformersNlpEngine; first use downloads the model once, then offline.

Custom pattern recognizers cover the AR identifiers Presidio lacks (MRN, claim #, bill ID, account #, member ID, auth #).
Existing tokens like [NAME-AB12CD] are never rescanned.
"""
import os
import re

from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer, RecognizerRegistry
from presidio_analyzer.nlp_engine import NlpEngineProvider

import phi_scrub as ps

ENTITIES = ["PERSON", "PHONE_NUMBER", "EMAIL_ADDRESS", "US_SSN", "US_DRIVER_LICENSE", "US_BANK_NUMBER",
            "MEDICAL_LICENSE", "IP_ADDRESS", "URL", "MRN", "AR_ID"]
MIN_SCORE = {"PERSON": 0.6}  # others: 0.4
# entity labels a clinical de-id transformer emits -> Presidio entity names
HF_MAP = {"PATIENT": "PERSON", "STAFF": "PERSON", "HCW": "PERSON", "PER": "PERSON", "PERSON": "PERSON",
          "PHONE": "PHONE_NUMBER", "ID": "AR_ID", "EMAIL": "EMAIL_ADDRESS", "VENDOR": "O", "HOSPITAL": "O",
          "DATE": "O", "AGE": "O", "LOC": "O", "LOCATION": "O", "PATORG": "O", "HOSP": "O", "ORGANIZATION": "O"}
TOKEN_RX = ps.TOKEN_RX
# payer / org / RCM words that NER mislabels as people; extended from the sheet's Payer/Client columns via allow()
ALLOW = {w.lower() for w in (
    "Aetna Cigna Humana Medicare Medicaid Anthem BCBS Molina Tricare Kaiser UHC UnitedHealthcare Optum Centene Wellcare "
    "Oscar Ambetter Highmark Priority Amerigroup Magellan Evernorth Availity Change Healthcare Waystar Claim Appeal Denial "
    "Provider Patient Payer Portal Status Balance Account Insurance Primary Secondary Corrected Medical Records "
    "Texas California Florida Ohio Arizona Georgia Virginia Oklahoma Colorado Nevada").split()}


def allow(values):
    for v in values:
        v = str(v).strip().lower()
        if v:
            ALLOW.add(v)
            ALLOW.update(w for w in re.split(r"[\s/&,-]+", v) if len(w) >= 3)

_engine = None


def _nlp_config():
    if os.environ.get("AR_AUDIT_NER", "spacy") == "hf":
        model = os.environ.get("AR_AUDIT_HF_MODEL", "StanfordAIMI/stanford-deidentifier-base")
        return {"nlp_engine_name": "transformers",
                "models": [{"lang_code": "en", "model_name": {"spacy": "en_core_web_sm", "transformers": model}}],
                "ner_model_configuration": {
                    "model_to_presidio_entity_mapping": HF_MAP,
                    "labels_to_ignore": ["O"], "aggregation_strategy": "simple",
                    "stride": 16, "alignment_mode": "expand", "low_confidence_score_multiplier": 0.4,
                    "low_score_entity_names": ["ID"]}}
    return {"nlp_engine_name": "spacy",
            "models": [{"lang_code": "en", "model_name": os.environ.get("AR_AUDIT_SPACY", "en_core_web_sm")}]}


def engine():
    global _engine
    if _engine is None:
        nlp = NlpEngineProvider(nlp_configuration=_nlp_config()).create_engine()
        reg = RecognizerRegistry()
        reg.load_predefined_recognizers(nlp_engine=nlp)
        reg.add_recognizer(PatternRecognizer(
            "MRN", patterns=[Pattern("mrn", r"\b(?:MRN|M\.R\.N\.?)\s*[:#-]?\s*[A-Z0-9-]{4,}\b", 0.85),
                             Pattern("mrn-bare", r"\b\d{6,10}\b", 0.3)],
            context=["mrn", "medical record", "chart"]))
        reg.add_recognizer(PatternRecognizer(
            "AR_ID", patterns=[Pattern("labelled-id", r"(?i)\b(?:claim|clm|icn|dcn|bill|acct|account|member|mbr|subscriber|auth|ref|case|batch|eft|check|call ref)\w*\s*(?:id|no|number|num|#)?\s*[:#-]?\s*(?=[A-Z0-9-]*\d)[A-Z0-9][A-Z0-9-]{5,}\b", 0.8),
                               Pattern("long-id", r"\b(?=[A-Z0-9-]*\d[A-Z0-9-]*\d[A-Z0-9-]*\d[A-Z0-9-]*\d)[A-Z0-9][A-Z0-9-]{6,}\b", 0.5)],
            context=["claim", "icn", "member", "auth", "ref", "account", "bill"]))
        _engine = AnalyzerEngine(nlp_engine=nlp, registry=reg, supported_languages=["en"])
    return _engine


def _scan(text):
    """Analyze one text; return non-overlapping (start, end, entity) spans above threshold, longest first."""
    res = engine().analyze(text=text, language="en", entities=ENTITIES)
    res = [r for r in res if r.score >= MIN_SCORE.get(r.entity_type, 0.4)
           and not (r.entity_type == "PERSON" and text[r.start:r.end].strip().lower() in ALLOW)]
    res.sort(key=lambda r: (-(r.end - r.start), -r.score))
    taken, out = [], []
    for r in res:
        if all(r.end <= s or r.start >= e for s, e in taken):
            taken.append((r.start, r.end))
            out.append((r.start, r.end, r.entity_type))
    return sorted(out)


def presidio_scrub(text):
    """Mask whatever Presidio/NER finds in the parts of `text` that are not already tokens."""
    parts = TOKEN_RX.split(str(text))
    for i in range(0, len(parts), 2):
        seg = parts[i]
        if len(seg.strip()) < 3:
            continue
        spans = _scan(seg)
        for s, e, ent in reversed(spans):
            kind = {"PERSON": "NAME", "PHONE_NUMBER": "PHONE", "EMAIL_ADDRESS": "EMAIL", "US_SSN": "SSN",
                    "IP_ADDRESS": "ID", "URL": "URL"}.get(ent, "ID")
            val = seg[s:e]
            if ps.KEEP.match(val.strip()):  # CPT/ICD/CARC-shaped codes are not identifiers
                continue
            seg = seg[:s] + ps.token(kind, val) + seg[e:]
        parts[i] = seg
    return "".join(parts)
