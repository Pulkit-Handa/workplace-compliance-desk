# Corpus and legal sources

## Principle

Only official instruments are indexed. Every provision is traceable to a PDF URL, a SHA-256 hash of the extracted source text, and a status/effective-date window. The corpus is deliberately small and reviewable rather than broad and unverifiable.

`corpus/sources/MANIFEST.json` is the provenance record. `corpus/provisions.jsonl` is the normalised, one-provision-per-line corpus. `scripts/build_corpus.py` regenerates the JSONL from the checked-in source text.

## Documents

| ID | Instrument | Status | Effective window | Provisions |
|---|---|---|---|---:|
| `ssc2020` | The Code on Social Security, 2020 | in force | 2025-11-21 → present | 27 |
| `posh2013` | The Sexual Harassment of Women at Workplace (Prevention, Prohibition and Redressal) Act, 2013 | in force | 2013-12-09 → present | 13 |
| `gratuity1972` | The Payment of Gratuity Act, 1972 | **repealed** | 1972-09-16 → 2025-11-20 | 4 |
| `commencement2025` | Commencement notifications for the four Labour Codes | in force | 2025-11-21 → present | 2 |

Total: **46 provisions**, **82 chunks** at `max_chars=1100`, `overlap_chars=120`.

### Official sources

| ID | URL | Source-text SHA-256 |
|---|---|---|
| `ssc2020` | https://www.labour.gov.in/static/uploads/2025/07/b0620548445580767b5c0d18c95c26f7.pdf | `5d3b3e292330dd0379e7dca45bc05b7fa7a80f2e4b9d14336d26c0f35a000fa5` |
| `posh2013` | https://doe.gov.in/files/inline-documents/DoE_Prevention_sexual_harassment.pdf | `7b7c795a24f23f9e3aab60cb983808c3d929aceffa22a4e83786b32f25a3db5e` |
| `gratuity1972` | https://www.labour.gov.in/static/uploads/2025/06/23e3be0e31de495acd1698ed85007ec1.pdf | `4a3947fb046c59b8090a47df6e9c89536e534350c6e40714173c4df78e014409` |
| `commencement2025` | https://labour.maharashtra.gov.in/sites/default/files/2025-11/implementation-of-labour-codes.pdf | `6961199bfe59a9c7cbc13853f7c86c3f8301f2143f49da8f6efc355f7eed40dc` |

The source text was extracted with `pdftotext -layout`, then normalised at provision level. The extraction is not blindly trusted: amendment footnotes, page headers, and duplicated section numbers are filtered by `scripts/build_corpus.py`. Any change to a source file or the normaliser changes the corpus hash and forces a new index version.

Verify a source hash:

```bash
sha256sum corpus/sources/ss_code_2020.txt
python3 - <<'PY'
import json
m = json.load(open("corpus/sources/MANIFEST.json"))
print(m["documents"][0]["source_sha256"])
PY
```

## Commencement

The Code on Social Security, 2020 was brought into force on **21 November 2025** by **S.O. 5319(E)**. The notification commences:

- sections 1–14;
- sub-sections (1) and (2) of section 15;
- clause (c) of sub-section (1) of section 16;
- sections 17–141;
- section 143, except the provisions specified in S.O. 2060(E) of 3 May 2023;
- sections 144–163;
- items 1 and 2 and items 4–9 of sub-section (1) of section 164;
- clauses (a) and (c) of sub-section (2), and sub-section (3), of section 164.

The parallel notifications are S.O. 5320(E) (Industrial Relations Code), S.O. 5321(E) (Occupational Safety, Health and Working Conditions Code), and S.O. 5322(E) (Code on Wages), all dated 21 November 2025. The corpus stores the Social Security notification and a compact record of the four notifications.

## Repeal mechanics

The Payment of Gratuity Act, 1972 is included **on purpose**, and is marked:

```json
{
  "status": "repealed",
  "effective_from": "1972-09-16",
  "effective_to": "2025-11-20",
  "repealed_by": "ssc2020"
}
```

Why retain it at all?

- A question can legitimately ask about a historical date (for example, a separation completed before 21 November 2025).
- The desk can then answer from the statute that was actually in force on that date.
- Keeping it also makes the boundary testable: a current answer must **not** use it as operative authority.

`SSC_COMMENCEMENT = date(2025, 11, 21)` in `src/compliance/tools/__init__.py` drives the branch. For `as_of_date >= 2025-11-21`, the gratuity query plan retrieves the Code and the commencement notification. For an earlier date, it retrieves the 1972 Act. `_in_force_on` in verification applies each chunk's `effective_from`/`effective_to` window.

Retrieval still sees the repealed provisions (so historical questions work), but it applies a status adjustment: in-force chunks get `+0.04`, repealed chunks get `-0.08`, and domain-matching chunks get `+0.07`. This is a ranking preference, not the safety control; the safety control is verification.

## Legal facts encoded in the corpus and policy

These are the provisions the evaluation and load tests exercise. They are described here for reviewers; the authoritative text is the indexed provision.

### Gratuity under the Code on Social Security, 2020

- **s. 53(1)** — gratuity is payable after five years of continuous service, on superannuation, retirement, resignation, expiry of a fixed-term contract, death, or disablement.
- **Second proviso to s. 53(1)** — the five-year requirement does not apply in the case of death or disablement, or on expiry of a fixed-term contract.
- **s. 53(2)** — the rate is fifteen days' wages for every completed year of service (or part above six months), based on the last drawn wage.
- **Third proviso to s. 53(2)** — a fixed-term employee is entitled on a pro-rata basis.
- **s. 54** — continuous service includes 240 days in a year, with the statutory treatment of seasons, casual work, and absence.
- **First Schedule, Chapter V** — applies the gratuity chapter to establishments with ten or more employees (subject to the Schedule's terms).
- **s. 164(1)** — repeals the Payment of Gratuity Act, 1972.

### Maternity benefit under the Code on Social Security, 2020

- **s. 60** — eligibility and duration, including 26 weeks for the first two children and 12 weeks in the other cases; the provision also addresses miscarriage, adoption, and commissioning mothers.
- **First Schedule, Chapter VI** — applies the maternity chapter at the ten-employee threshold.
- **s. 67** — crèche facility for establishments with fifty or more employees.
- **s. 68** — protection against dismissal or discharge during or on account of maternity absence; appeal within sixty days.

### POSH Act, 2013

- **s. 4** — every employer must constitute an Internal Complaints Committee by order in writing.
- **s. 9** — a complaint must ordinarily be made within three months of the incident; the Committee may extend the period for reasons recorded in writing.
- **s. 11** — inquiry into the complaint.
- **s. 26** — penalty for non-compliance, including a fine up to ₹50,000.
- POSH was not repealed by the four Labour Codes and remains in force.

### Out of scope

The corpus does not contain minimum-wage rates, general service law, or a complete set of labour-code provisions. A question outside the indexed material is routed to `legal_counsel_review`; the desk does not answer from model memory.

## Rebuilding the corpus and index

```bash
python3 scripts/build_corpus.py          # source text → corpus/provisions.jsonl
python3 -m compliance.cli index build    # corpus → versioned hybrid index
python3 -m compliance.cli index info     # current version, hashes, freshness
```

The index build reuses unchanged vectors from the previous index and the SQLite embedding cache. A changed corpus, chunking config, or embedding fingerprint forces a new index version. The previous two versions are kept for rollback; `current.json` is the pointer.

## Corpus limitations

- The corpus is deliberately narrow: it is a compliance desk for the listed topics, not a general legal database.
- The source text is an extraction. The manifest and hashes make it auditable, but the official PDF remains the authoritative instrument.
- State rules, rules made under the Code, and case law are not indexed. A question that depends on them escalates.
- Effective dates are modelled at document/provision level from the commencement notification; the corpus does not attempt to encode every amendment history.
