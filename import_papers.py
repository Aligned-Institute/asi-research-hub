#!/usr/bin/env python3
"""
One-shot import of papers.json into the ARP v2 PostgreSQL database.
Run from /opt/arp with the venv activated.
"""
import json
import os
from datetime import datetime
from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import execute_values

load_dotenv()

DATABASE_URL = os.getenv('DATABASE_URL')
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set")

PAPERS_FILE = os.path.join(os.path.dirname(__file__), 'papers.json')

def run():
    with open(PAPERS_FILE) as f:
        papers = json.load(f)

    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    now = datetime.utcnow()

    imported = 0
    skipped = 0
    tag_cache = {}

    for p in papers:
        source_id = p.get('arxiv_id') or p.get('doi') or p.get('filename') or p['title'][:80]
        source = 'arxiv' if p.get('arxiv_id') else 'manual'

        # Skip duplicates
        cur.execute("SELECT id FROM papers WHERE source=%s AND source_id=%s", (source, source_id))
        if cur.fetchone():
            skipped += 1
            continue

        arxiv_id = p.get('arxiv_id') or None
        doi = p.get('doi') or None
        if arxiv_id:
            url = f"https://arxiv.org/abs/{arxiv_id}"
        elif doi:
            url = f"https://doi.org/{doi}"
        else:
            url = None

        tags_json = json.dumps(p.get('tags') or [])

        cur.execute("""
            INSERT INTO papers (title, authors, abstract, year, source, source_id,
                                arxiv_id, doi, url, citation_count, tags, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            p['title'],
            p.get('authors', ''),
            p.get('abstract', ''),
            p.get('year'),
            source,
            source_id,
            arxiv_id,
            doi,
            url,
            p.get('citation_count', 0) or 0,
            tags_json,
            now,
        ))
        paper_id = cur.fetchone()[0]

        for tag_name in (p.get('tags') or []):
            tag_name = tag_name.strip()
            if not tag_name:
                continue
            if tag_name not in tag_cache:
                cur.execute("""
                    INSERT INTO tags (name, frequency, paper_count, created_at)
                    VALUES (%s, 0, 0, %s)
                    ON CONFLICT (name) DO UPDATE SET frequency = tags.frequency
                    RETURNING id
                """, (tag_name, now))
                tag_cache[tag_name] = cur.fetchone()[0]

            cur.execute("""
                INSERT INTO paper_tags (paper_id, tag_id, created_at)
                VALUES (%s, %s, %s)
                ON CONFLICT DO NOTHING
            """, (paper_id, tag_cache[tag_name], now))

        imported += 1

    # Update tag frequencies
    cur.execute("""
        UPDATE tags SET frequency = (
            SELECT COUNT(*) FROM paper_tags WHERE paper_tags.tag_id = tags.id
        )
    """)

    conn.commit()
    cur.close()
    conn.close()
    print(f"Done — imported: {imported}, skipped (duplicates): {skipped}, tags: {len(tag_cache)}")

if __name__ == '__main__':
    run()
