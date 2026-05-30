"""
ArXiv API connector.
Provides access to preprint papers in physics, mathematics, CS, and other fields.
API Docs: https://info.arxiv.org/help/api/index.html
"""

import re
import requests
import feedparser
from typing import Optional, List, Dict, Any
from datetime import datetime
from urllib.parse import urlencode
from flask import current_app

_REQUEST_TIMEOUT = 30


class ArxivConnector:
    """
    Connector for ArXiv API.

    Free API with no authentication required.
    Rate limit: Max 1 request every 3 seconds (enforced client-side).
    """

    BASE_URL = "http://export.arxiv.org/api/query"
    TIMEOUT = 30  # seconds

    # ArXiv categories relevant to AI safety
    AI_SAFETY_CATEGORIES = [
        'cs.AI',  # Artificial Intelligence
        'cs.LG',  # Machine Learning
        'cs.CL',  # Computation and Language
        'cs.CV',  # Computer Vision
        'cs.CY',  # Computers and Society
        'cs.HC',  # Human-Computer Interaction
        'stat.ML',  # Machine Learning (Statistics)
    ]

    def __init__(self):
        """Initialize ArXiv connector."""
        pass

    def search_papers(
        self,
        query: str,
        max_results: int = 10,
        start: int = 0,
        sort_by: str = 'relevance',
        sort_order: str = 'descending'
    ) -> Dict[str, Any]:
        """
        Search for papers on ArXiv.

        Args:
            query: Search query (supports AND, OR, ANDNOT, field prefixes)
            max_results: Maximum number of results (default: 10, recommended max: 100)
            start: Starting index for pagination
            sort_by: Sort criteria ('relevance', 'lastUpdatedDate', 'submittedDate')
            sort_order: Sort order ('ascending', 'descending')

        Returns:
            dict: {
                'total': int,
                'start': int,
                'papers': List[dict]
            }

        Example queries:
            - "all:machine learning"
            - "ti:interpretability AND cat:cs.AI"
            - "au:bengio AND abs:alignment"
        """
        if not query or not query.strip():
            raise ValueError("Search query cannot be empty")

        # Build query parameters
        params = {
            'search_query': query.strip(),
            'start': start,
            'max_results': min(max_results, 100),  # Limit to 100 per request
            'sortBy': sort_by,
            'sortOrder': sort_order
        }

        url = f"{self.BASE_URL}?{urlencode(params)}"

        try:
            # Parse the Atom feed response
            resp = requests.get(url, timeout=_REQUEST_TIMEOUT)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)

            # Check for errors
            if feed.bozo and not feed.entries:
                error_msg = getattr(feed, 'bozo_exception', 'Unknown error')
                current_app.logger.error(f"ArXiv feed parsing error: {error_msg}")
                raise Exception(f"Failed to parse ArXiv feed: {error_msg}")

            # Extract total results from opensearch namespace
            total_results = int(feed.feed.get('opensearch_totalresults', 0))
            start_index = int(feed.feed.get('opensearch_startindex', 0))

            papers = [self._normalize_paper(entry) for entry in feed.entries]

            return {
                'total': total_results,
                'start': start_index,
                'papers': papers
            }

        except Exception as e:
            current_app.logger.error(f"ArXiv search error: {e}")
            raise Exception(f"ArXiv search failed: {str(e)}")

    def get_paper(self, arxiv_id: str) -> Optional[Dict[str, Any]]:
        """
        Get detailed information about a specific paper by ArXiv ID.

        Args:
            arxiv_id: ArXiv identifier (e.g., '2103.00020' or 'arXiv:2103.00020')

        Returns:
            dict: Normalized paper data or None if not found
        """
        if not arxiv_id:
            raise ValueError("ArXiv ID cannot be empty")

        # Clean the ID (remove 'arXiv:' prefix if present)
        clean_id = arxiv_id.replace('arXiv:', '').strip()

        # Search by ID
        params = {
            'id_list': clean_id,
            'max_results': 1
        }

        url = f"{self.BASE_URL}?{urlencode(params)}"

        try:
            resp = requests.get(url, timeout=_REQUEST_TIMEOUT)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)

            if not feed.entries:
                return None

            return self._normalize_paper(feed.entries[0])

        except Exception as e:
            current_app.logger.error(f"ArXiv get_paper error: {e}")
            raise Exception(f"Failed to get ArXiv paper: {str(e)}")

    def search_by_category(
        self,
        category: str,
        max_results: int = 10,
        start: int = 0
    ) -> Dict[str, Any]:
        """
        Search papers in a specific ArXiv category.

        Args:
            category: ArXiv category code (e.g., 'cs.AI', 'cs.LG')
            max_results: Maximum number of results
            start: Starting index for pagination

        Returns:
            dict: Search results
        """
        query = f"cat:{category}"
        return self.search_papers(query, max_results, start, sort_by='submittedDate')

    def search_ai_safety_papers(
        self,
        max_results: int = 50,
        start: int = 0
    ) -> Dict[str, Any]:
        """
        Convenience method to search for AI safety and alignment papers on ArXiv.

        Args:
            max_results: Maximum number of results
            start: Starting index for pagination

        Returns:
            dict: Search results
        """
        # Build comprehensive search query
        keywords = [
            'interpretability',
            'alignment',
            'AI safety',
            'machine learning safety',
            'adversarial robustness',
            'explainability',
            'RLHF',
            'mechanistic interpretability',
        ]

        # Search in title, abstract, and comments
        query_parts = [f'(all:{kw})' for kw in keywords]
        query = ' OR '.join(query_parts)

        # Limit to relevant categories
        category_query = ' OR '.join([f'cat:{cat}' for cat in self.AI_SAFETY_CATEGORIES])
        final_query = f"({query}) AND ({category_query})"

        return self.search_papers(final_query, max_results, start)

    def _normalize_paper(self, entry: Any) -> Dict[str, Any]:
        """
        Normalize ArXiv entry to common format.

        Args:
            entry: Feedparser entry object

        Returns:
            dict: Normalized paper data matching our Paper model
        """
        # Extract ArXiv ID from the entry ID
        arxiv_id = entry.id.split('/abs/')[-1] if '/abs/' in entry.id else entry.id

        # Extract authors
        authors = [author.get('name', '') for author in entry.get('authors', [])]

        # Parse publication/update dates
        published = entry.get('published')
        updated = entry.get('updated')

        published_date = None
        if published:
            try:
                published_date = datetime.strptime(published, '%Y-%m-%dT%H:%M:%SZ').date()
            except (ValueError, TypeError):
                pass

        # Extract year
        year = published_date.year if published_date else None

        # Extract categories/tags
        categories = []
        if hasattr(entry, 'tags'):
            categories = [tag.get('term', '') for tag in entry.tags]

        # Extract primary category
        primary_category = entry.get('arxiv_primary_category', {}).get('term')

        # Get DOI if available
        doi = None
        if hasattr(entry, 'arxiv_doi'):
            doi = entry.arxiv_doi

        # Extract PDF link
        pdf_url = None
        for link in entry.get('links', []):
            if link.get('type') == 'application/pdf':
                pdf_url = link.get('href')
                break

        # Extract journal reference if available
        journal_ref = entry.get('arxiv_journal_ref')

        # Get comment (often contains conference/workshop info)
        comment = entry.get('arxiv_comment')

        return {
            'source': 'arxiv',
            'source_id': arxiv_id,
            'arxiv_id': arxiv_id,
            'doi': doi,
            'title': entry.get('title', '').strip(),
            'abstract': entry.get('summary', '').strip() if entry.get('summary') else None,
            'authors': authors,
            'year': year,
            'published_date': published_date,
            'updated_date': updated,
            'venue': journal_ref,  # Use journal reference as venue
            'primary_category': primary_category,
            'categories': categories,
            'comment': comment,
            'pdf_url': pdf_url,
            'url': entry.id,
            'citation_count': 0,  # ArXiv doesn't provide citation counts
            'fields_of_study': categories,  # Use categories as fields
            'raw_data': {
                'categories': categories, # Explicitly put in raw_data for tagger
                **dict(entry)
            }  # Store full entry for reference
        }

    @staticmethod
    def build_query(
        title: Optional[str] = None,
        author: Optional[str] = None,
        abstract: Optional[str] = None,
        category: Optional[str] = None,
        all_fields: Optional[str] = None
    ) -> str:
        """
        Helper to build ArXiv search queries with proper field prefixes.

        Args:
            title: Search in title (prefix: ti:)
            author: Search in author (prefix: au:)
            abstract: Search in abstract (prefix: abs:)
            category: Search in category (prefix: cat:)
            all_fields: Search in all fields (prefix: all:)

        Returns:
            str: Formatted query string

        Example:
            >>> ArxivConnector.build_query(title="alignment", category="cs.AI")
            'ti:alignment AND cat:cs.AI'
        """
        parts = []

        if title:
            parts.append(f'ti:{title}')
        if author:
            parts.append(f'au:{author}')
        if abstract:
            parts.append(f'abs:{abstract}')
        if category:
            parts.append(f'cat:{category}')
        if all_fields:
            parts.append(f'all:{all_fields}')

        return ' AND '.join(parts) if parts else ''
