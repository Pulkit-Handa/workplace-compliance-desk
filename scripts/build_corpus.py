#!/usr/bin/env python3
"""Build the normalised provision corpus from official source text.

The source files in ``corpus/sources`` were extracted with ``pdftotext -layout``
from the official PDFs linked in ``corpus/sources/MANIFEST.json``.  This script
turns them into one JSON object per provision.  It is deliberately boring and
reviewable: every emitted provision can be checked against the source text.

Run:  python scripts/build_corpus.py
Out:  corpus/provisions.jsonl
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "corpus" / "sources"
OUT = ROOT / "corpus" / "provisions.jsonl"

SOURCES = {
    "ssc2020": {
        "file": "ss_code_2020.txt",
        "title": "The Code on Social Security, 2020",
        "short_title": "Code on Social Security, 2020",
        "instrument_type": "code",
        "status": "in_force",
        "effective_from": "2025-11-21",
        "effective_to": None,
        "repealed_by": None,
        "source_url": "https://www.labour.gov.in/static/uploads/2025/07/b0620548445580767b5c0d18c95c26f7.pdf",
        "source_note": "Gazette of India, No. 36 of 2020; commencement S.O. 5319(E) dated 21 Nov 2025",
    },
    "gratuity1972": {
        "file": "payment_of_gratuity_act_1972.txt",
        "title": "The Payment of Gratuity Act, 1972",
        "short_title": "Payment of Gratuity Act, 1972",
        "instrument_type": "repealed_act",
        "status": "repealed",
        "effective_from": "1972-09-16",
        "effective_to": "2025-11-20",
        "repealed_by": "ssc2020",
        "source_url": "https://www.labour.gov.in/static/uploads/2025/06/23e3be0e31de495acd1698ed85007ec1.pdf",
        "source_note": "Act No. 39 of 1972; repealed by Code on Social Security, 2020, s. 164 w.e.f. 21 Nov 2025",
    },
    "posh2013": {
        "file": "posh_act_2013.txt",
        "title": "The Sexual Harassment of Women at Workplace (Prevention, Prohibition and Redressal) Act, 2013",
        "short_title": "POSH Act, 2013",
        "instrument_type": "act",
        "status": "in_force",
        "effective_from": "2013-12-09",
        "effective_to": None,
        "repealed_by": None,
        "source_url": "https://doe.gov.in/files/inline-documents/DoE_Prevention_sexual_harassment.pdf",
        "source_note": "Act No. 14 of 2013; in force 9 Dec 2013; not repealed by the four Labour Codes",
    },
    "commencement2025": {
        "file": "labour_codes_commencement_2025.txt",
        "title": "Commencement notifications for the four Labour Codes",
        "short_title": "Labour Codes Commencement Notification, 2025",
        "instrument_type": "notification",
        "status": "in_force",
        "effective_from": "2025-11-21",
        "effective_to": None,
        "repealed_by": None,
        "source_url": "https://labour.maharashtra.gov.in/sites/default/files/2025-11/implementation-of-labour-codes.pdf",
        "source_note": "S.O. 5319(E) (Code on Social Security), S.O. 5320(E), 5321(E), 5322(E), all dated 21 Nov 2025",
    },
}

HEADER_RE = re.compile(
    r"^\s*(?:\d+\s+)?THE GAZETTE OF INDIA|^\s*SEC\.\s*\d+\]|^\s*PART II[—-]|^\s*\d+\s*$"
)
FOOTNOTE_RE = re.compile(
    r"^\s*\d+\.\s*(?:Subs\.|Ins\.|Added|Omitted|The words|In section|Renumbered|Repealed|"
    r"[Ee]xplanation inserted|Clause|Schedule|Vide|Notification|w\.e\.f\.)",
    re.I,
)
SECTION_RE = re.compile(
    r"^\s*\[?\s*(?:[A-Z][A-Za-z ,.&'’()\-]{0,34}\s{2,})?(\d{1,3}[A-Z]?)\.\s+(?=[A-Z“\"(])"
)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def clean_body(text: str) -> str:
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if HEADER_RE.search(line):
            continue
        if FOOTNOTE_RE.search(line):
            continue
        line = re.sub(r"^\s*\d+\s+THE GAZETTE OF INDIA.*$", "", line)
        line = re.sub(r"^\s*SEC\.\s*\d+\]\s*", "", line)
        # Strip the right-hand margin heading glued to the first line.
        line = re.sub(r"\s{3,}(?:Short title|Definitions|Payment of|Constitution|Duties|Penalty|"
                      r"Repeal|Application|Commencement|Right to|Notice of|Power of|Nomination|"
                      r"Continuous|Appeal|Wages|Establishment|Employee|Maternity|Gratuity).*$", "", line)
        lines.append(line)
    # collapse runs of blank lines, join hyphenated line breaks
    out: list[str] = []
    blank = False
    for line in lines:
        if not line.strip():
            if not blank:
                out.append("")
            blank = True
            continue
        blank = False
        if out and out[-1].endswith("-") and line[:1].islower():
            out[-1] = out[-1][:-1] + line.lstrip()
        else:
            out.append(line.strip())
    return "\n".join(out).strip()


def extract_sections(path: Path) -> dict[str, dict]:
    text = path.read_text(encoding="utf-8")
    start = text.find("BE it enacted")
    if start < 0:
        start = text.find("MINISTRY OF LABOUR AND EMPLOYMENT")
    body = text[start:]
    lines = body.splitlines()
    hits: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        m = SECTION_RE.match(line)
        if not m:
            continue
        if any(
            token in line
            for token in (
                "Subs. by",
                "Ins. by",
                "Added by",
                "Omitted",
                "vide notification",
                "Gazette of India",
                "THE GAZETTE",
                "Renumbered",
            )
        ):
            continue
        # Amendment notes at the foot of a page are numbered like sections and
        # would otherwise overwrite the real section with a footnote fragment.
        if re.match(r"^\s*\d+\.\s+(?:Section|Sub-section|Clause)\b", line):
            continue
        hits.append((i, m.group(1)))
    sections: dict[str, dict] = {}
    for n, (line_no, number) in enumerate(hits):
        # Keep the first occurrence: footnotes and cross-references may repeat a
        # section number later in the document, but the first body heading wins.
        if str(number) in sections:
            continue
        end = hits[n + 1][0] if n + 1 < len(hits) else len(lines)
        raw = "\n".join(lines[line_no:end])
        cleaned = clean_body(raw)
        # remove the section number prefix from the first line
        first, _, rest = cleaned.partition(".")
        heading = ""
        if "—" in rest:
            heading, _, rest = rest.partition("—")
            heading = heading.strip()
        text_body = (heading + ".—" + rest).strip() if heading else rest.strip()
        sections[str(number)] = {
            "heading": heading,
            "text": text_body,
            "raw": cleaned,
        }
    return sections


def add(
    items: list[dict],
    doc_id: str,
    section: str,
    clause: str,
    heading: str,
    text: str,
    *,
    topics: list[str] | None = None,
    related: list[str] | None = None,
    citation: str | None = None,
    page: int | None = None,
) -> None:
    src = SOURCES[doc_id]
    stem = re.sub(r"[^a-z0-9]+", "-", section.lower()).strip("-")
    pid = f"{doc_id}-s{stem}"
    if clause:
        pid += "-" + re.sub(r"[^a-z0-9]+", "-", clause.lower()).strip("-")
    items.append(
        {
            "id": pid,
            "document_id": doc_id,
            "statute": src["title"],
            "short_title": src["short_title"],
            "instrument_type": src["instrument_type"],
            "section": section,
            "clause": clause,
            "heading": heading,
            "text": re.sub(r"\s+", " ", text).strip(),
            "citation": citation or f"{src['short_title']}, s. {section}{(' (' + clause + ')') if clause else ''}",
            "status": src["status"],
            "effective_from": src["effective_from"],
            "effective_to": src["effective_to"],
            "repealed_by": src["repealed_by"],
            "source_url": src["source_url"],
            "source_doc_sha256": sha256((SRC / src["file"]).read_text(encoding="utf-8")),
            "page": page,
            "topics": topics or [],
            "related_ids": related or [],
        }
    )


def build() -> list[dict]:
    items: list[dict] = []
    ss = extract_sections(SRC / "ss_code_2020.txt")
    posh = extract_sections(SRC / "posh_act_2013.txt")
    grat = extract_sections(SRC / "payment_of_gratuity_act_1972.txt")

    # ------------------------------------------------------------------ SS Code
    add(
        items,
        "ssc2020",
        "1(4)",
        "",
        "Application of Chapters through the First Schedule",
        "The applicability of the Chapters specified in columns (1) and (2) of the First Schedule shall, "
        "without prejudice to the applicability of the other provisions of this Code, be such as is "
        "specified in corresponding entry in column (3) of that Schedule.",
        topics=["application", "establishment", "coverage"],
        related=["ssc2020-sfirst-schedule-chapter-v", "ssc2020-sfirst-schedule-chapter-vi"],
    )
    add(
        items,
        "ssc2020",
        "2(26)",
        "",
        "Definition of employee",
        '"employee" means any person (other than an apprentice engaged under the Apprentices Act, 1961) '
        "employed on wages by an establishment, either directly or through a contractor, to do any skilled, "
        "semi-skilled or unskilled, manual, operational, supervisory, managerial, administrative, technical, "
        "clerical or any other work, whether the terms of employment be express or implied, and also includes "
        "a person declared to be an employee by the appropriate Government, but does not include any member "
        "of the Armed Forces of the Union.",
        topics=["definition", "employee", "gratuity", "maternity"],
    )
    add(
        items,
        "ssc2020",
        "2(29)",
        "",
        "Definition of establishment",
        '"establishment" means (a) a place where any industry, trade, business, manufacture or occupation is '
        "carried on; or (b) a factory, motor transport undertaking, newspaper establishment, audio-visual "
        "production, building and other construction work or plantation; or (c) a mine, port or vicinity of "
        "port where dock work is carried out.",
        topics=["definition", "establishment", "coverage"],
    )
    add(
        items,
        "ssc2020",
        "2(34)",
        "",
        "Definition of fixed term employment",
        '"fixed term employment" means the engagement of an employee on the basis of a written contract of '
        "employment for a fixed period: Provided that— (a) his hours of work, wages, allowances and other "
        "benefits shall not be less than that of a permanent employee doing the same work or work of a similar "
        "nature; and (b) he shall be eligible for all benefits, under any law for the time being in force, "
        "available to a permanent employee proportionately according to the period of service rendered by him "
        "even if his period of employment does not extend to the required qualifying period of employment.",
        topics=["definition", "fixed_term_employment", "gratuity", "proportional_benefits"],
        related=["ssc2020-s53-1-second-proviso", "ssc2020-s53-2-third-proviso"],
    )
    add(
        items,
        "ssc2020",
        "53(1)",
        "",
        "Payment of gratuity — main clause",
        "Gratuity shall be payable to an employee on the termination of his employment after he has rendered "
        "continuous service for not less than five years,— (a) on his superannuation; or (b) on his retirement "
        "or resignation; or (c) on his death or disablement due to accident or disease; or (d) on termination of "
        "his contract period under fixed term employment; or (e) on happening of any such event as may be "
        "notified by the Central Government.",
        topics=["gratuity", "five_years", "continuous_service", "fixed_term_employment", "termination"],
        related=[
            "ssc2020-s53-1-first-proviso",
            "ssc2020-s53-1-second-proviso",
            "ssc2020-s53-2",
            "ssc2020-s53-2-third-proviso",
            "ssc2020-s54",
            "ssc2020-s164-1",
            "ssc2020-sfirst-schedule-chapter-v",
            "gratuity1972-s4",
        ],
        page=41,
    )
    add(
        items,
        "ssc2020",
        "53(1)",
        "first proviso",
        "Payment of gratuity — working journalist proviso",
        'Provided that in case of working journalist as defined in clause (f) of section 2 of the Working '
        'Journalists and Other Newspaper Employees (Condition of Service) and Miscellaneous Provisions Act, '
        '1955, the expression "five years" occurring in this sub-section shall be deemed to be three years.',
        topics=["gratuity", "working_journalist", "five_years", "proviso"],
        related=["ssc2020-s53-1"],
        page=41,
    )
    add(
        items,
        "ssc2020",
        "53(1)",
        "second proviso",
        "Payment of gratuity — five years not necessary on death, disablement or expiry of fixed term",
        "Provided further that the completion of continuous service of five years shall not be necessary where "
        "the termination of the employment of any employee is due to death or disablement or expiration of fixed "
        "term employment or happening of any such event as may be notified by the Central Government.",
        topics=[
            "gratuity",
            "fixed_term_employment",
            "fixed_term_expiry",
            "continuous_service",
            "five_years",
            "exception",
            "proviso",
        ],
        related=["ssc2020-s53-1", "ssc2020-s53-2-third-proviso", "ssc2020-s2-34", "ssc2020-s54"],
        page=41,
    )
    add(
        items,
        "ssc2020",
        "53(2)",
        "",
        "Payment of gratuity — rate of gratuity",
        "For every completed year of service or part thereof in excess of six months, the employer shall pay "
        "gratuity to an employee at the rate of fifteen days' wages or such number of days as may be notified by "
        "the Central Government, based on the rate of wages last drawn by the employee concerned.",
        topics=["gratuity", "calculation", "fifteen_days", "wages"],
        related=["ssc2020-s53-2-third-proviso", "ssc2020-s53-1"],
        page=41,
    )
    add(
        items,
        "ssc2020",
        "53(2)",
        "third proviso",
        "Payment of gratuity — pro rata for fixed term employment",
        "Provided also that in the case of an employee employed on fixed term employment or a deceased employee, "
        "the employer shall pay gratuity on pro rata basis.",
        topics=["gratuity", "fixed_term_employment", "pro_rata", "calculation", "proviso"],
        related=["ssc2020-s53-2", "ssc2020-s53-1-second-proviso"],
        page=42,
    )
    add(
        items,
        "ssc2020",
        "53(6)",
        "",
        "Payment of gratuity — forfeiture",
        "Notwithstanding anything contained in sub-section (1),— (a) the gratuity of an employee, whose services "
        "have been terminated for any act, wilful omission or negligence causing any damage or loss to, or "
        "destruction of, property belonging to the employer, shall be forfeited to the extent of the damage or "
        "loss so caused; (b) the gratuity payable to an employee may be wholly or partially forfeited— (i) if the "
        "services of such employee have been terminated for his riotous or disorderly conduct or any other act "
        "of violence on his part, or (ii) if the services of such employee have been terminated for any act which "
        "constitutes an offence involving moral turpitude, provided such offence is committed by him in the "
        "course of his employment.",
        topics=["gratuity", "forfeiture", "termination", "misconduct"],
        related=["ssc2020-s53-1"],
    )

    # Continuous service, auto-extracted but cleaned
    sec54 = ss.get("54")
    if sec54:
        add(
            items,
            "ssc2020",
            "54",
            "",
            "Continuous service",
            sec54["raw"],
            topics=["gratuity", "continuous_service", "240_days", "definition"],
            related=["ssc2020-s53-1", "ssc2020-s53-1-second-proviso"],
        )

    add(
        items,
        "ssc2020",
        "First Schedule",
        "Chapter V",
        "First Schedule — applicability of Chapter V (Gratuity)",
        "Chapter V (Gratuity) applies to: (a) every factory, mine, oilfield, plantation, port and railway "
        "company; and (b) every shop or establishment in which ten or more employees are employed, or were "
        "employed, on any day of the preceding twelve months; and such shops or establishments as may be "
        "notified by the appropriate Government from time to time.",
        topics=["gratuity", "application", "coverage", "ten_employees", "establishment"],
        related=["ssc2020-s1-4", "ssc2020-s53-1"],
    )
    add(
        items,
        "ssc2020",
        "First Schedule",
        "Chapter VI",
        "First Schedule — applicability of Chapter VI (Maternity Benefit)",
        "Chapter VI (Maternity Benefit) applies to: (a) every establishment being a factory, mine or plantation "
        "including any such establishment belonging to Government; and (b) every shop or establishment in which "
        "ten or more employees are employed, or were employed, on any day of the preceding twelve months; and "
        "such other shops or establishments notified by the appropriate Government.",
        topics=["maternity_benefit", "application", "coverage", "ten_employees", "establishment"],
        related=["ssc2020-s1-4", "ssc2020-s60"],
    )

    # Maternity benefit chapter (auto)
    for num in ["60", "61", "62", "63", "64", "65", "66", "67", "68", "69", "70", "71", "72"]:
        sec = ss.get(num)
        if not sec:
            continue
        topics = ["maternity_benefit"]
        if num == "60":
            topics += ["eligibility", "eighty_days", "twenty_six_weeks", "payment"]
        if num == "67":
            topics += ["creche", "fifty_employees"]
        if num == "68":
            topics += ["dismissal", "pregnancy", "protection"]
        related = ["ssc2020-sfirst-schedule-chapter-vi"]
        if num != "60":
            related.insert(0, "ssc2020-s60")
        add(
            items,
            "ssc2020",
            num,
            "",
            sec["heading"] or f"Section {num}",
            sec["raw"],
            topics=topics,
            related=related,
        )

    add(
        items,
        "ssc2020",
        "164(1)",
        "",
        "Repeal and savings — repealed enactments",
        "The following enactments are hereby repealed, namely:— 1. The Employee's Compensation Act, 1923; "
        "2. The Employees' State Insurance Act, 1948; 3. The Employees' Provident Funds and Miscellaneous "
        "Provisions Act, 1952; 4. The Employment Exchanges (Compulsory Notification of Vacancies) Act, 1959; "
        "5. The Maternity Benefit Act, 1961; 6. The Payment of Gratuity Act, 1972; 7. The Cine-Workers Welfare "
        "Fund Act, 1981; 8. The Building and Other Construction Workers' Welfare Cess Act, 1996; "
        "9. The Unorganised Workers' Social Security Act, 2008.",
        topics=["repeal", "payment_of_gratuity_act_1972", "maternity_benefit_act_1961", "in_force"],
        related=["gratuity1972-s4", "commencement2025-ss-o-5319-e"],
        page=98,
    )

    # --------------------------------------------------------------- POSH Act
    posh_keep = {
        "1": ["application", "commencement"],
        "2": ["definition", "sexual_harassment", "workplace", "employee", "employer", "internal_committee"],
        "3": ["sexual_harassment", "prevention", "hostile_work_environment"],
        "4": ["internal_committee", "constitution", "employer_duty"],
        "9": ["complaint", "three_months", "internal_committee", "limitation"],
        "10": ["conciliation", "settlement"],
        "11": ["inquiry", "internal_committee", "procedure"],
        "13": ["inquiry_report", "action"],
        "14": ["false_complaint", "punishment"],
        "16": ["confidentiality"],
        "19": ["employer_duty", "safe_environment", "training", "internal_committee"],
        "21": ["annual_report"],
        "26": ["penalty", "non_compliance", "internal_committee"],
    }
    posh_related = {
        "4": ["posh2013-s19", "posh2013-s26", "posh2013-s9"],
        "9": ["posh2013-s4", "posh2013-s11", "posh2013-s19"],
        "19": ["posh2013-s4", "posh2013-s9", "posh2013-s26"],
        "26": ["posh2013-s4", "posh2013-s19"],
    }
    for num, topics in posh_keep.items():
        sec = posh.get(num)
        if not sec:
            continue
        add(
            items,
            "posh2013",
            num,
            "",
            sec["heading"] or f"Section {num}",
            sec["raw"],
            topics=topics,
            related=posh_related.get(num, []),
        )

    # ------------------------------------------------------------ Gratuity Act
    for num, topics in {
        "1": ["application", "coverage", "ten_employees", "repealed"],
        "2A": ["continuous_service", "repealed"],
        "4": ["gratuity", "five_years", "continuous_service", "repealed"],
        "14": ["override", "repealed"],
    }.items():
        sec = grat.get(num)
        if not sec:
            continue
        add(
            items,
            "gratuity1972",
            num,
            "",
            sec["heading"] or f"Section {num}",
            sec["raw"],
            topics=topics + ["repealed_act"],
            related=["ssc2020-s53-1", "ssc2020-s164-1", "commencement2025-ss-o-5319-e"],
        )

    # ------------------------------------------------------- Commencement 2025
    add(
        items,
        "commencement2025",
        "S.O. 5319(E)",
        "",
        "Commencement of the Code on Social Security, 2020",
        "MINISTRY OF LABOUR AND EMPLOYMENT NOTIFICATION New Delhi, the 21st November, 2025. S.O. 5319(E).—In "
        "exercise of the powers conferred by sub-section (3) of section 1 of the Code on Social Security, 2020 "
        "(36 of 2020), the Central Government hereby appoints the 21st day of November, 2025 as the date on "
        "which the following provisions of the said Code shall come into force, namely: sections 1 to 14; "
        "sub-sections (1) and (2) of section 15; clause (c) of sub-section (1) of section 16; sections 17 to 141; "
        "section 143 (except specified provisions); sections 144 to 163; and specified items of section 164.",
        topics=["commencement", "in_force", "social_security_code", "21_november_2025"],
        related=["ssc2020-s164-1", "ssc2020-s53-1"],
    )
    add(
        items,
        "commencement2025",
        "S.O. 5322(E)",
        "",
        "Commencement of the Code on Wages, 2019",
        "MINISTRY OF LABOUR AND EMPLOYMENT NOTIFICATION New Delhi, the 21st November, 2025. S.O. 5322(E).—In "
        "exercise of the powers conferred by sub-section (3) of section 1 of the Code on Wages, 2019 (29 of "
        "2019), the Central Government hereby appoints the 21st day of November, 2025 as the date on which the "
        "provisions of the said Code shall come into force.",
        topics=["commencement", "in_force", "code_on_wages", "21_november_2025"],
    )

    # Link related ids to real ids (best effort)
    ids = {item["id"] for item in items}
    for item in items:
        item["related_ids"] = [r for r in item["related_ids"] if r in ids]
    # Add inverse links
    by_id = {item["id"]: item for item in items}
    for item in items:
        for rel in list(item["related_ids"]):
            other = by_id.get(rel)
            if other and item["id"] not in other["related_ids"]:
                other["related_ids"].append(item["id"])

    # The layout-based extraction leaves right-margin headings inside the
    # maternity provisions.  Those provisions are transcribed cleanly here so
    # that a quotation from the index is readable and can be checked against the
    # Gazette.  Text is the operative body only; the section number is metadata.
    overrides: dict[str, dict[str, str]] = {
        "ssc2020-s60": {
            "heading": "Right to payment of maternity benefit",
            "text": (
                "(1) Subject to the other provisions of this Code, every woman shall be entitled to, and her "
                "employer shall be liable for, the payment of maternity benefit at the rate of the average daily wage "
                "for the period of her actual absence, that is to say, the period immediately preceding the day of her "
                "delivery, and any period immediately following that day. Explanation.—For the purposes of this "
                "sub-section, \"the average daily wage\" means the average of the woman's wages payable to her for the "
                "days on which she has worked during the period of three calendar months immediately preceding the date "
                "from which she absents herself on account of maternity, subject to the minimum rate of wage fixed or "
                "revised under the Code on Wages, 2019. (2) No woman shall be entitled to maternity benefit unless she "
                "has actually worked in an establishment of the employer from whom she claims maternity benefit, for a "
                "period of not less than eighty days in the twelve months immediately preceding the date of her expected "
                "delivery. Explanation.—For the purposes of calculating the period under this sub-section, the days on "
                "which a woman has actually worked in the establishment, the days for which she has been laid off or was "
                "on holidays declared under any law for the time being in force to be holidays with wages, during the "
                "period of twelve months immediately preceding the expected date of her delivery shall be taken into "
                "account. (3) The maximum period for which any woman shall be entitled to maternity benefit shall be "
                "twenty-six weeks of which not more than eight weeks shall precede the expected date of her delivery: "
                "Provided that the maximum period entitled to maternity benefit by a woman having two or more surviving "
                "children shall be twelve weeks of which not more than six weeks shall precede the date of her expected "
                "delivery: Provided further that where a woman dies during this period, the maternity benefit shall be "
                "payable only for the days up to and including the day of her death: Provided also that where a woman, "
                "having been delivered of a child, dies during her delivery or during the period immediately following "
                "the date of her delivery for which she is entitled for the maternity benefit, leaving behind in either "
                "case the child, the employer shall be liable for the maternity benefit for that entire period but if "
                "the child also dies during the said period, then, for the days up to and including the date of the death "
                "of the child. Explanation.—For the purposes of this sub-section, \"child\" includes a stillborn child. "
                "(4) A woman who legally adopts a child below the age of three months or a commissioning mother shall be "
                "entitled to maternity benefit for a period of twelve weeks from the date the child is handed over to the "
                "adopting mother or the commissioning mother, as the case may be. (5) In case the work assigned to a woman "
                "is of such nature that she may work from home, the employer may allow her to do so after availing of the "
                "maternity benefit for such period and on such conditions as the employer and the woman may mutually agree."
            ),
        },
        "ssc2020-s61": {
            "heading": "Continuance of payment of maternity benefit in certain cases",
            "text": (
                "Every woman entitled to the payment of maternity benefit under this Chapter shall, notwithstanding "
                "the application of Chapter IV to the factory or other establishment in which she is employed, "
                "continue to be so entitled until she becomes qualified to claim maternity benefit under section 32."
            ),
        },
        "ssc2020-s62": {
            "heading": "Notice of claim for maternity benefit and payment thereof",
            "text": (
                "(1) Any woman employed in an establishment and entitled to maternity benefit under the provisions of "
                "this Chapter may give notice in writing in such form as may be prescribed by the Central Government, "
                "to her employer, stating that her maternity benefit and any other amount to which she may be entitled "
                "under this Chapter may be paid to her or to such person as she may nominate in the notice and that she "
                "will not work in any establishment during the period for which she receives maternity benefit. "
                "(2) In the case of a woman who is pregnant, such notice shall state the date from which she will be "
                "absent from work, not being a date earlier than eight weeks from the date of her expected delivery. "
                "(3) Any woman who has not given the notice when she was pregnant may give such notice as soon as "
                "possible after her delivery. (4) On receipt of the notice, the employer shall permit such woman to "
                "absent herself from the establishment during the period for which she receives the maternity benefit. "
                "(5) The amount of maternity benefit for the period preceding the date of her expected delivery shall "
                "be paid in advance by the employer to the woman on production of such proof as may be prescribed by "
                "the Central Government that the woman is pregnant, and the amount due for the subsequent period shall "
                "be paid by the employer to the woman within forty-eight hours of production of such proof as may be "
                "prescribed by the Central Government that the woman has been delivered of a child. (6) The failure to "
                "give notice under this section shall not disentitle a woman to maternity benefit or any other amount "
                "under this Chapter if she is otherwise entitled to such benefit or amount and in any such case an "
                "Inspector-cum-Facilitator may either of his own motion or on an application made to him by the woman, "
                "order the payment of such benefit or amount within such period as may be specified in the order."
            ),
        },
        "ssc2020-s63": {
            "heading": "Payment of maternity benefit in case of death of a woman",
            "text": (
                "If a woman entitled to maternity benefit or any other amount under this Chapter, dies before "
                "receiving such maternity benefit or amount, or where the employer is liable for maternity benefit "
                "under the second proviso to sub-section (3) of section 60, the employer shall pay such benefit or "
                "amount to the person nominated by the woman in the notice given under section 62 and in case there is "
                "no such nominee, to her legal representative."
            ),
        },
        "ssc2020-s64": {
            "heading": "Payment of medical bonus",
            "text": (
                "Every woman entitled to maternity benefit under this Chapter shall also be entitled to receive from "
                "her employer a medical bonus of three thousand five hundred rupees or such amount as may be notified "
                "by the Central Government, if no pre-natal confinement and post-natal care is provided for by the "
                "employer free of charge."
            ),
        },
        "ssc2020-s65": {
            "heading": "Leave for miscarriage, etc.",
            "text": (
                "(1) In case of miscarriage, or medical termination of pregnancy, a woman shall, on production of such "
                "proof as may be prescribed by the Central Government, be entitled to leave with wages at the rate of "
                "maternity benefit, for a period of six weeks immediately following the day of her miscarriage or, as "
                "the case may be, her medical termination of pregnancy. (2) In case of tubectomy operation, a woman "
                "shall, on production of such proof as may be prescribed by the Central Government, be entitled to "
                "leave with wages at the rate of maternity benefit for a period of two weeks immediately following the "
                "day of her tubectomy operation. (3) A woman suffering from illness arising out of pregnancy, delivery, "
                "premature birth of child, miscarriage or medical termination of pregnancy shall, on production of such "
                "proof as may be prescribed by the Central Government, be entitled, in addition to the period of "
                "absence allowed to her under section 62, or, as the case may be, under sub-section (1), to leave with "
                "wages at the rate of maternity benefit for a maximum period of one month."
            ),
        },
        "ssc2020-s66": {
            "heading": "Nursing breaks",
            "text": (
                "Every woman delivered of a child who returns to duty after such delivery shall, in addition to the "
                "interval for rest allowed to her, be allowed in the course of her daily work two breaks of such "
                "duration as may be prescribed by the Central Government, for nursing the child until the child attains "
                "the age of fifteen months."
            ),
        },
        "ssc2020-s67": {
            "heading": "Creche facility",
            "text": (
                "(1) Every establishment to which this Chapter applies, in which fifty employees or such number of "
                "employees as may be prescribed by the Central Government, are employed shall have the facility of crèche "
                "within such distance as may be prescribed by the Central Government, either separately or along with "
                "common facilities: Provided that the employer shall allow four visits a day to the crèche by the woman, "
                "which shall also include the intervals of rest allowed to her: Provided further that an establishment may "
                "avail common crèche facility of the Central Government, State Government, municipality or private entity "
                "or provided by non-Governmental organisation or by any other organisation or group of establishments who "
                "may pool their resources for setting up of common crèche in the manner as they may agree for such "
                "purpose. (2) Every establishment to which this Chapter applies shall intimate in writing and "
                "electronically to every woman at the time of her initial appointment in such establishment regarding "
                "every benefit available under this Chapter."
            ),
        },
        "ssc2020-s68": {
            "heading": "Dismissal for absence during pregnancy",
            "text": (
                "(1) When a woman absents herself from work in accordance with the provisions of this Chapter, it shall "
                "be unlawful for her employer to discharge or dismiss her during or on account of such absence or to give "
                "notice of discharge or dismissal on such a day that the notice will expire during such absence, or to "
                "vary to her disadvantage any of the conditions of her service: Provided that the discharge or dismissal "
                "of a woman at any time during her pregnancy, if the woman but for such discharge or dismissal would have "
                "been entitled to maternity benefit or medical bonus under this Chapter, shall not have the effect of "
                "depriving her of the maternity benefit or medical bonus: Provided further that where the dismissal is "
                "for any gross misconduct as may be prescribed by the Central Government, the employer may, by order in "
                "writing, communicated to the woman, deprive her of the maternity benefit or medical bonus, or both. "
                "(2) Any woman deprived of maternity benefit or medical bonus, or both, or discharged or dismissed under "
                "sub-section (1), may, within sixty days from the date on which order of such deprivation or discharge or "
                "dismissal is communicated to her, appeal to the competent authority, and the decision of that authority "
                "on such appeal, whether the woman should or should not be deprived of maternity benefit or medical bonus "
                "or both, or discharged or dismissed, shall be final."
            ),
        },
        "ssc2020-s69": {
            "heading": "No deduction of wages in certain cases",
            "text": (
                "No deduction from the normal and usual daily wages of a woman entitled to maternity benefit under the "
                "provisions of this Chapter shall be made by reason only of— (a) the nature of work assigned to her by "
                "virtue of the provisions contained in section 59; or (b) breaks for nursing the child allowed to her "
                "under the provisions of section 66."
            ),
        },
        "ssc2020-s70": {
            "heading": "Forfeiture of maternity benefit",
            "text": (
                "A woman who works for remuneration during the period she has been permitted by an employer to absent "
                "herself for availing the maternity benefits provided under this Chapter shall not be entitled to "
                "receive maternity benefit for such period."
            ),
        },
        "ssc2020-s71": {
            "heading": "Duties of employer",
            "text": (
                "An abstract of the provisions of this Chapter and the rules relating thereto in the language or "
                "languages of the locality shall be exhibited in a conspicuous place by the employer in every part of "
                "the establishment in which women are employed."
            ),
        },
        "ssc2020-s72": {
            "heading": "Power of Inspector-cum-Facilitator to direct payments to be made",
            "text": (
                "(1) Any woman claiming that,— (a) maternity benefit or any other amount to which she is entitled under "
                "this Chapter and any person claiming that payment due under this Chapter has been improperly withheld; "
                "(b) her employer has discharged or dismissed her during or on account of her absence from work in "
                "accordance with the provisions of this Chapter, may make a complaint to the Inspector-cum-Facilitator. "
                "(2) The Inspector-cum-Facilitator may, on receipt of a complaint referred to in sub-section (1), make an "
                "inquiry or cause an inquiry to be made and if satisfied that— (a) payment has been wrongfully withheld, "
                "may direct the payment to be made in accordance with his order in writing; (b) she has been discharged "
                "or dismissed during or on account of her absence from work in accordance with the provisions of this "
                "Chapter, may pass such orders as he deems just and proper according to the circumstances of the case. "
                "(3) Any person aggrieved by the order of the Inspector-cum-Facilitator under sub-section (2) may, within "
                "thirty days from the date on which such order is communicated to such person, appeal to the authority "
                "prescribed by the appropriate Government. (4) The decision of the authority referred to in sub-section "
                "(3), where an appeal has been preferred to it under that sub-section or of the Inspector-cum-Facilitator "
                "where no such appeal has been preferred, shall be final."
            ),
        },
        "gratuity1972-s1": {
            "heading": "Short title, extent, application and commencement",
            "text": (
                "(1) This Act may be called the Payment of Gratuity Act, 1972. (2) It extends to the whole of India: "
                "Provided that in so far as it relates to plantations or ports, it shall not extend to the State of "
                "Jammu and Kashmir. (3) It shall apply to— (a) every factory, mine, oilfield, plantation, port and "
                "railway company; (b) every shop or establishment within the meaning of any law for the time being in "
                "force in relation to shops and establishments in a State, in which ten or more persons are employed, "
                "or were employed, on any day of the preceding twelve months; (c) such other establishments or class of "
                "establishments, in which ten or more employees are employed, or were employed, on any day of the "
                "preceding twelve months, as the Central Government may, by notification, specify in this behalf. "
                "(3A) A shop or establishment to which this Act has become applicable shall continue to be governed by "
                "this Act notwithstanding that the number of persons employed therein at any time after it has become "
                "so applicable falls below ten. (4) It shall come into force on such date as the Central Government may, "
                "by notification, appoint."
            ),
        },
    }
    for item in items:
        override = overrides.get(item["id"])
        if override:
            item["heading"] = override["heading"]
            item["text"] = override["text"]

    # add order and sort
    for i, item in enumerate(items):
        item["order"] = i
    return items


def main() -> None:
    items = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as fh:
        for item in items:
            fh.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
    docs = {}
    for item in items:
        docs.setdefault(item["document_id"], 0)
        docs[item["document_id"]] += 1
    print(f"wrote {len(items)} provisions to {OUT.relative_to(ROOT)}")
    for doc, count in docs.items():
        print(f"  {doc}: {count}")


if __name__ == "__main__":
    main()
