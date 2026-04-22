import asyncio
from backend.database import db

async def check():
    coll = db['chat_history']
    platforms = await coll.distinct('platform')
    total = await coll.count_documents({})
    print(f"Platforms: {platforms}")
    print(f"Total docs: {total}")
    
    # Check first few entries to see what 'platform' looks like
    cursor = coll.find().limit(5)
    async for doc in cursor:
        print(f"Doc: platform={doc.get('platform')}, timestamp={doc.get('timestamp')}")

if __name__ == "__main__":
    asyncio.run(check())
