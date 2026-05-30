"""Add raw_data and missing columns to papers table

Revision ID: 002
Revises: 001
Create Date: 2025-12-28

Adds:
- raw_data JSONB column to papers table for source-specific metadata (categories, fieldsOfStudy, subjects)
- Missing columns: doi, arxiv_id, pdf_path, pdf_text, citation_count, asip_funded, added_by, tags, venue
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision = '002'
down_revision = '001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add missing columns to papers table
    # Check if columns exist before adding (for idempotency)

    # Add DOI column
    op.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                      WHERE table_name='papers' AND column_name='doi') THEN
            ALTER TABLE papers ADD COLUMN doi VARCHAR(255);
        END IF;
    END $$;
    """)

    # Add ArXiv ID column
    op.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                      WHERE table_name='papers' AND column_name='arxiv_id') THEN
            ALTER TABLE papers ADD COLUMN arxiv_id VARCHAR(100);
        END IF;
    END $$;
    """)

    # Add venue column
    op.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                      WHERE table_name='papers' AND column_name='venue') THEN
            ALTER TABLE papers ADD COLUMN venue TEXT;
        END IF;
    END $$;
    """)

    # Add PDF path column
    op.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                      WHERE table_name='papers' AND column_name='pdf_path') THEN
            ALTER TABLE papers ADD COLUMN pdf_path VARCHAR(255);
        END IF;
    END $$;
    """)

    # Add PDF text column (for full-text search)
    op.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                      WHERE table_name='papers' AND column_name='pdf_text') THEN
            ALTER TABLE papers ADD COLUMN pdf_text TEXT;
        END IF;
    END $$;
    """)

    # Add citation count column (if not exists from initial schema)
    op.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                      WHERE table_name='papers' AND column_name='citation_count') THEN
            ALTER TABLE papers ADD COLUMN citation_count INTEGER DEFAULT 0;
        END IF;
    END $$;
    """)

    # Add ASIP funded flag
    op.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                      WHERE table_name='papers' AND column_name='asip_funded') THEN
            ALTER TABLE papers ADD COLUMN asip_funded BOOLEAN DEFAULT FALSE;
        END IF;
    END $$;
    """)

    # Add added_by column (who added the paper)
    op.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                      WHERE table_name='papers' AND column_name='added_by') THEN
            ALTER TABLE papers ADD COLUMN added_by VARCHAR(255);
        END IF;
    END $$;
    """)

    # Add tags column (JSON array of tag strings)
    op.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                      WHERE table_name='papers' AND column_name='tags') THEN
            ALTER TABLE papers ADD COLUMN tags TEXT;
        END IF;
    END $$;
    """)

    # Add raw_data JSONB column (THE MAIN FIX FOR TAG ISSUE)
    op.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                      WHERE table_name='papers' AND column_name='raw_data') THEN
            ALTER TABLE papers ADD COLUMN raw_data JSONB;
            COMMENT ON COLUMN papers.raw_data IS 'Source-specific metadata: ArXiv categories, Semantic Scholar fieldsOfStudy, CrossRef subjects, etc.';
        END IF;
    END $$;
    """)

    # Add missing columns to tags table
    # Add slug column
    op.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                      WHERE table_name='tags' AND column_name='slug') THEN
            ALTER TABLE tags ADD COLUMN slug VARCHAR(100) UNIQUE;
        END IF;
    END $$;
    """)

    # Add category column
    op.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                      WHERE table_name='tags' AND column_name='category') THEN
            ALTER TABLE tags ADD COLUMN category VARCHAR(50);
        END IF;
    END $$;
    """)

    # Add description column
    op.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                      WHERE table_name='tags' AND column_name='description') THEN
            ALTER TABLE tags ADD COLUMN description TEXT;
        END IF;
    END $$;
    """)

    # Add last_used column
    op.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                      WHERE table_name='tags' AND column_name='last_used') THEN
            ALTER TABLE tags ADD COLUMN last_used TIMESTAMP;
        END IF;
    END $$;
    """)

    # Add paper_count column
    op.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                      WHERE table_name='tags' AND column_name='paper_count') THEN
            ALTER TABLE tags ADD COLUMN paper_count INTEGER DEFAULT 0;
        END IF;
    END $$;
    """)

    # Add confidence column to paper_tags
    op.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                      WHERE table_name='paper_tags' AND column_name='confidence') THEN
            ALTER TABLE paper_tags ADD COLUMN confidence DECIMAL(3, 2);
        END IF;
    END $$;
    """)

    # Create index on raw_data for efficient querying
    op.execute("""
    CREATE INDEX IF NOT EXISTS idx_papers_raw_data ON papers USING gin (raw_data);
    """)


def downgrade() -> None:
    # Drop added columns in reverse order
    op.drop_index('idx_papers_raw_data', table_name='papers', if_exists=True)

    op.execute("ALTER TABLE papers DROP COLUMN IF EXISTS raw_data;")
    op.execute("ALTER TABLE papers DROP COLUMN IF EXISTS tags;")
    op.execute("ALTER TABLE papers DROP COLUMN IF EXISTS added_by;")
    op.execute("ALTER TABLE papers DROP COLUMN IF EXISTS asip_funded;")
    op.execute("ALTER TABLE papers DROP COLUMN IF EXISTS citation_count;")
    op.execute("ALTER TABLE papers DROP COLUMN IF EXISTS pdf_text;")
    op.execute("ALTER TABLE papers DROP COLUMN IF EXISTS pdf_path;")
    op.execute("ALTER TABLE papers DROP COLUMN IF EXISTS venue;")
    op.execute("ALTER TABLE papers DROP COLUMN IF EXISTS arxiv_id;")
    op.execute("ALTER TABLE papers DROP COLUMN IF EXISTS doi;")

    op.execute("ALTER TABLE tags DROP COLUMN IF EXISTS paper_count;")
    op.execute("ALTER TABLE tags DROP COLUMN IF EXISTS last_used;")
    op.execute("ALTER TABLE tags DROP COLUMN IF EXISTS description;")
    op.execute("ALTER TABLE tags DROP COLUMN IF EXISTS category;")
    op.execute("ALTER TABLE tags DROP COLUMN IF EXISTS slug;")

    op.execute("ALTER TABLE paper_tags DROP COLUMN IF EXISTS confidence;")
