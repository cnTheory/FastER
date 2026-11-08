"""Databricks scenario endpoint for the existing starter dashboard."""
from typing import Literal
from fastapi import APIRouter
from pydantic import BaseModel, Field
from . import demo_scoring as scoring, demo_ranking_data as data, demo_ranking_backend as backend

router=APIRouter(prefix='/api/demo-ranking',tags=['Databricks demo ranking'])
class Request(BaseModel):
    symptoms: list[Literal['chest','breath','bleed','unresp','stroke','fracture','burn','cut','fever','pain','sprain','cold']] = Field(default_factory=list,max_length=12)
    age: int = Field(default=25,ge=0,le=120)
    pain: int = Field(default=5,ge=0,le=10)
    scenario_id: Literal['baseline','queue_surge','staff_reduction','capability_change']='baseline'
    peds: bool=False
    trauma: bool=False
    insurance: str | None=Field(default=None,max_length=80)

@router.post('')
async def compare(r:Request):
    # Always query Databricks here, regardless of the legacy DATA_MODE setting.
    hs,inc,outcome_fn,meta=await data.load(r.scenario_id)
    syms=list(dict.fromkeys(r.symptoms))
    level=scoring.severity(syms,r.age,r.pain)
    ranked=await backend.rank(hs,inc,outcome_fn,level,syms,r.insurance,r.peds or r.age<18,r.trauma)
    by_id={h['id']:h for h in hs}
    for row in ranked:
        h=by_id[row['hospital_id']]
        row.update(facility_id=h['facility_id'],latitude=h['latitude'],longitude=h['longitude'],
                   observed_at=h['observed_at'],queue_count=sum(h['q']),boarding=h['boarding'])
    best=ranked[0] if ranked else None
    explanation=(f"In the selected simulation, {best['name']} has the lowest algorithm score ({best['score']}). "
                 f"Its estimated travel plus wait is {best['estimated_total_min']} minutes. "
                 "Ranking also includes the original compatibility adjustments; these operational values are simulated."
                 if best else 'No comparison is available.')
    return {**meta,'ranking':ranked,'best_hospital_id':best['hospital_id'] if best else None,
            'level':level,'call_911':bool(scoring.RED_FLAGS & set(syms)) or level<=2,
            'explanation':explanation,'algorithm_version':'demo-v1-unchanged',
            'notice':'Real hospital directory; simulated operational data fetched from Databricks. Not live availability.'}
