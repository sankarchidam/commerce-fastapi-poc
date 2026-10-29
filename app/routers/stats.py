from __future__ import annotations

from fastapi import APIRouter, Depends

from app.deps import DbDep, SessionDep, require_api_key
from app.models import StatsOverview

router = APIRouter(prefix="/stats", tags=["stats"], dependencies=[Depends(require_api_key)])


@router.get("/overview", response_model=StatsOverview, summary="Headline counts, GMV, refunds, last webhook time")
def overview(s: SessionDep, db: DbDep) -> StatsOverview:
    return StatsOverview.model_validate(s.fetch_one(db.query("stats_overview")))
