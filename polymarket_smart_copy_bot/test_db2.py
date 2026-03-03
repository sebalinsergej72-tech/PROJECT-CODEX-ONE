import asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from config.settings import settings
from models.models import PortfolioSnapshot

async def run():
    engine = create_async_engine(settings.async_database_url)
    session_maker = async_sessionmaker(engine)
    async with session_maker() as session:
        query = select(PortfolioSnapshot).order_by(PortfolioSnapshot.taken_at.desc()).limit(5)
        res = await session.execute(query)
        snapshots = res.scalars().all()
        for s in snapshots:
            print(f"[{s.taken_at}] Equity: {s.total_equity_usd} Exposure: {s.exposure_usd}")
        print("Done")

if __name__ == "__main__":
    asyncio.run(run())
