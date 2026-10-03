"""Detection metrics for labelled samples (ground truth: ok / failure)."""
from collections import Counter, defaultdict


def is_positive(status: str, positives: set[str]) -> bool:
    return status in positives


def confusion(rows: list[dict], positives: set[str]) -> dict:
    """rows: {'truth': 'ok'|'failure', 'truth_issue': str, 'status': model status, 'issue': model issue}."""
    c = Counter()
    by_issue = defaultdict(lambda: [0, 0])          # truth issue -> [detected, total]
    for r in rows:
        truth = r["truth"] == "failure"
        pred = is_positive(r["status"], positives)
        c[("tp" if pred else "fn") if truth else ("fp" if pred else "tn")] += 1
        if truth:
            by_issue[r.get("truth_issue", "other")][1] += 1
            by_issue[r.get("truth_issue", "other")][0] += pred
    tp, fp, fn, tn = c["tp"], c["fp"], c["fn"], c["tn"]

    def ratio(a, b):
        return round(a / b, 3) if b else None

    return {"n": tp + fp + fn + tn, "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "recall": ratio(tp, tp + fn),                 # share of real failures detected
            "precision": ratio(tp, tp + fp),              # share of alarms that were real
            "false_alarm_rate": ratio(fp, fp + tn),       # share of good frames flagged
            "accuracy": ratio(tp + tn, tp + fp + fn + tn),
            "recall_by_issue": {k: f"{v[0]}/{v[1]}" for k, v in sorted(by_issue.items())}}


def format_report(name: str, m: dict) -> str:
    def pct(x):
        return "  n/a" if x is None else f"{x * 100:5.1f}%"
    lines = [
        f"{name}: n={m['n']}   TP={m['tp']} FN={m['fn']} FP={m['fp']} TN={m['tn']}",
        f"   recall (failures caught) {pct(m['recall'])}   precision {pct(m['precision'])}   "
        f"false-alarm rate {pct(m['false_alarm_rate'])}   accuracy {pct(m['accuracy'])}",
    ]
    if m["recall_by_issue"]:
        lines.append("   recall by issue: " + ", ".join(f"{k} {v}" for k, v in m["recall_by_issue"].items()))
    return "\n".join(lines)
