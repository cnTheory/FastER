"""Pure functions: severity, wait estimate, hospital ranking. No I/O."""
WEIGHTS = {"chest": 4, "breath": 4, "bleed": 4, "unresp": 5, "stroke": 5, "fracture": 2,
           "burn": 2, "cut": 1, "fever": 1, "pain": 2, "sprain": 0, "cold": 0}
SYM_COND = {"chest": "cardiac", "breath": "respiratory", "bleed": "trauma", "unresp": "neuro",
            "stroke": "neuro", "fracture": "trauma", "burn": "trauma", "cut": "trauma",
            "fever": "infection", "pain": "abdominal", "sprain": "trauma", "cold": "respiratory"}
RED_FLAGS = {"chest", "breath", "bleed", "unresp", "stroke"}


def severity(symptoms, age=None, pain=None):
    s = sum(WEIGHTS[k] for k in symptoms)
    pain = pain or 0
    s += 2 if pain >= 8 else 1 if pain >= 5 else 0
    if age is not None and (age < 2 or age > 75):
        s += 1
    lvl = 1 if s >= 7 else 2 if s >= 5 else 3 if s >= 3 else 4 if s >= 1 else 5
    if RED_FLAGS & set(symptoms):  # hard safety override, independent of any model
        lvl = min(lvl, 2)
    return lvl


def primary_condition(symptoms):
    return SYM_COND[max(symptoms, key=lambda k: WEIGHTS[k])] if symptoms else None


def est_wait(h, lvl, incoming_levels):
    """Minutes. q[i] = patients waiting at level i+1; staff-adjusted."""
    if lvl == 1:
        return 0
    ahead = sum(h["q"][:lvl]) + sum(1 for l in incoming_levels if l <= lvl)
    svc = h["svc"][min(lvl, 4)]
    crowding = 1 + 0.04 * min(h.get("boarding", 0), 10)  # boarded admits tie up ED beds and staff
    return round(ahead * svc / max(h["staff"], 1) * 1.5 * crowding + (5 if lvl >= 4 else 2))


def travel_min(h):
    return round(h["miles"] * 1.8 + 2)  # replace with a routing API (OSRM / Google Routes)


def compat(h, ins, peds, trauma):
    ok, bad = [], []
    if ins and ins != "Not sure":
        (ok if ins in h["insurance"] else bad).append(
            f"accepts {ins}" if ins in h["insurance"] else f"may not accept {ins}")
    if peds:
        (ok if h["peds"] else bad).append("pediatric care" if h["peds"] else "no pediatric unit")
    if trauma:
        (ok if h["trauma"] else bad).append("trauma center" if h["trauma"] else "not a trauma center")
    return ok, bad


def rank(hospitals, incoming, outcome_fn, lvl, symptoms, ins=None, peds=False, trauma=False):
    cond = primary_condition(symptoms)
    out = []
    for h in hospitals:
        w, t = est_wait(h, lvl, incoming.get(h["id"], [])), travel_min(h)
        ok, bad = compat(h, ins, peds, trauma)
        oi = outcome_fn(h.get("facility_id", h["id"]), cond) if cond else 1.0
        penalty = round((oi - 1) * 80) if lvl <= 3 else 0  # worse-than-avg outcomes cost up to ~80 min
        total = w + t + 40 * len(bad) + penalty - (15 if lvl <= 2 and h["trauma"] else 0)
        out.append({"hospital_id": h["id"], "name": h["name"], "city": h["city"], "drive_min": t,
                    "wait_min": w, "score": total, "estimated_total_min": w + t,  # honest ETA; ranking uses "score" (adds penalties/bonuses)
                    "outcome_index": round(oi, 2), "condition": cond, "staff_on": h["staff"],
                    "compatible": ok, "warnings": bad})
    return sorted(out, key=lambda r: r["score"])
