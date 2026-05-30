"""
Paper endpoints for ARA v2.
Handles paper search, retrieval, and management.
"""

from flask import Blueprint, request, jsonify, current_app, g
from ara_v2.models.paper import Paper
from ara_v2.models.tag import Tag
from ara_v2.models.paper_tag import PaperTag
from ara_v2.models.citation import Citation
from ara_v2.middleware.auth import require_auth, optional_auth, get_current_user
from ara_v2.utils.database import db
from ara_v2.utils.errors import ValidationError, NotFoundError, ARAError, ConflictError
from ara_v2.utils.rate_limiter import limiter
from ara_v2.services.paper_ingestion import PaperIngestionService
from sqlalchemy import func, or_, and_
from werkzeug.utils import secure_filename
from typing import Optional
import PyPDF2
import os
import requests
import tempfile
from datetime import datetime
from urllib.parse import urlparse

papers_bp = Blueprint('papers', __name__)


# Helper functions for file upload
def extract_pdf_text(pdf_path: str) -> str:
    """Extract text from PDF using PyPDF2."""
    with open(pdf_path, 'rb') as file:
        reader = PyPDF2.PdfReader(file)
        text = ""
        for page in reader.pages:
            page_text = page.extract_text()
            if page_text:
                text += page_text + "\n"
        return text.strip()


def allowed_file(filename: str) -> bool:
    """Check if file is PDF."""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() == 'pdf'


def download_pdf_from_url(url: str, save_dir: str) -> str:
    """
    Download PDF from URL and validate it's accessible.

    Args:
        url: URL to download PDF from
        save_dir: Directory to save the PDF

    Returns:
        str: Path to the downloaded file

    Raises:
        ValidationError: If URL is invalid or not accessible
    """
    # Validate URL format
    try:
        parsed = urlparse(url)
        if not all([parsed.scheme, parsed.netloc]):
            raise ValidationError('Invalid URL format')
        if parsed.scheme not in ['http', 'https']:
            raise ValidationError('URL must use HTTP or HTTPS protocol')
    except Exception as e:
        raise ValidationError(f'Invalid URL: {str(e)}')

    # Download the file
    try:
        response = requests.get(url, timeout=30, stream=True, allow_redirects=True)
        response.raise_for_status()

        # Check file size (max 50MB)
        content_length = response.headers.get('Content-Length')
        if content_length and int(content_length) > 50 * 1024 * 1024:
            raise ValidationError('PDF file too large (max 50MB)')

        # Generate filename from URL
        filename = os.path.basename(urlparse(url).path)
        if not filename or not filename.endswith('.pdf'):
            filename = 'paper.pdf'
        filename = secure_filename(filename)

        # Save file
        timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
        filename = f"{timestamp}_{filename}"
        filepath = os.path.join(save_dir, filename)

        with open(filepath, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)

        return filepath

    except requests.exceptions.Timeout:
        raise ValidationError('URL request timed out - server did not respond')
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            raise ValidationError('URL not found (404)')
        elif e.response.status_code == 403:
            raise ValidationError('Access forbidden (403) - URL may not be publicly accessible')
        else:
            raise ValidationError(f'HTTP error {e.response.status_code}')
    except requests.exceptions.RequestException as e:
        raise ValidationError(f'Failed to download from URL: {str(e)}')


def check_duplicate_by_title(title: str) -> Optional[Paper]:
    """Check if paper with similar title exists."""
    return Paper.query.filter(db.func.lower(Paper.title) == title.lower()).first()


@papers_bp.route('/search', methods=['POST'])
@optional_auth
@limiter.limit("30 per minute")
def search():
    """
    Search for papers across multiple sources.

    Request body:
        {
            "query": "AI safety",
            "sources": ["semantic_scholar", "arxiv", "crossref", "google_scholar"],  # Optional, defaults to all
            "max_results": 20,  # Optional, default: 20
            "ingest": true,  # Optional, whether to save to DB, default: false
            "assign_tags": true  # Optional, auto-assign tags, default: true
        }

    Available sources:
        - semantic_scholar: Free API, 200M+ papers
        - arxiv: Free API, STEM preprints
        - crossref: Free API, comprehensive metadata
        - google_scholar: SerpAPI required (SERPAPI_API_KEY), with arXiv fallback on timeout

    Returns:
        {
            "total_fetched": int,
            "total_ingested": int,  # If ingest=true
            "papers": List[dict],
            "warnings": List[dict]  # Optional, if any source failed
        }
    """
    try:
        data = request.get_json()

        if not data:
            raise ValidationError('Request body is required')

        query = data.get('query', '').strip()
        if not query:
            raise ValidationError('Query parameter is required')

        sources = data.get('sources', ['semantic_scholar', 'arxiv', 'crossref', 'google_scholar'])
        max_results = data.get('max_results', 20)
        ingest = data.get('ingest', False)
        assign_tags = data.get('assign_tags', True)

        # Validate max_results
        if max_results < 1 or max_results > 100:
            raise ValidationError('max_results must be between 1 and 100')

        # Initialize ingestion service
        ingestion_service = PaperIngestionService()

        if ingest:
            # Search and ingest papers
            result = ingestion_service.search_and_ingest(
                query=query,
                sources=sources,
                max_results_per_source=max_results,
                assign_tags=assign_tags
            )

            papers_data = [paper.to_dict() for paper in result['papers']]

            return jsonify({
                'total_fetched': result['total_fetched'],
                'total_ingested': result['total_ingested'],
                'new_papers': result['new_papers'],
                'duplicates_found': result['duplicates_found'],
                'fetch_stats': result['fetch_stats'],
                'papers': papers_data
            }), 200

        else:
            # Search only (don't save to database)
            all_papers = []
            warnings = []

            if 'internal' in sources:
                try:
                    # Search internal database
                    search_pattern = f'%{query}%'
                    db_papers = Paper.query.filter(
                        or_(
                            Paper.title.ilike(search_pattern),
                            Paper.abstract.ilike(search_pattern),
                            Paper.authors.ilike(search_pattern)
                        )
                    ).order_by(Paper.created_at.desc()).limit(max_results).all()
                    
                    # Convert to dict format
                    for paper in db_papers:
                        paper_dict = paper.to_dict()
                        paper_dict['source'] = 'internal'
                        all_papers.append(paper_dict)
                    
                    current_app.logger.info(f"Internal DB search returned {len(db_papers)} papers for query: {query}")
                except Exception as e:
                    current_app.logger.error(f"Internal database search error: {e}")
                    warnings.append({
                        'source': 'internal',
                        'message': f'Internal database search failed: {str(e)}'
                    })

            if 'semantic_scholar' in sources:
                # Semantic Scholar with CrossRef fallback on timeout/rate limit
                try:
                    current_app.logger.info(f"Searching Semantic Scholar for: {query}")
                    s2_result = ingestion_service.s2_connector.search_papers(
                        query, limit=max_results
                    )
                    papers_count = len(s2_result.get('papers', []))
                    ss_papers = s2_result.get('papers', [])
                    all_papers.extend(ss_papers)
                    if ss_papers:
                        current_app.logger.info(f"✓ Semantic Scholar returned {papers_count} papers - First paper keys: {list(ss_papers[0].keys())}")
                    else:
                        current_app.logger.info(f"✓ Semantic Scholar returned {papers_count} papers")
                except Exception as e:
                    error_msg = str(e).lower()
                    current_app.logger.error(f"Semantic Scholar search error (will check for timeout/rate limit): {e}")
                    
                    # Check if it's a timeout or rate limit error
                    if 'timeout' in error_msg or 'timed out' in error_msg or '429' in str(e):
                        if 'timeout' in error_msg or 'timed out' in error_msg:
                            current_app.logger.warning(f"⚠️ Semantic Scholar search timed out: {e}")
                        else:
                            current_app.logger.warning(f"⚠️ Semantic Scholar rate limited (429): {e}")
                        
                        current_app.logger.info(f"→ Falling back to CrossRef for query: {query}")
                        
                        try:
                            # Fallback to CrossRef
                            crossref_fallback_result = ingestion_service.crossref_connector.search_papers(
                                query, rows=max_results
                            )
                            fallback_papers = crossref_fallback_result.get('papers', [])
                            all_papers.extend(fallback_papers)
                            current_app.logger.info(f"✓ CrossRef fallback returned {len(fallback_papers)} papers")
                            
                            warnings.append({
                                'source': 'semantic_scholar',
                                'message': 'Semantic Scholar unavailable - results from CrossRef'
                            })
                        except Exception as crossref_error:
                            current_app.logger.error(f"CrossRef fallback also failed: {crossref_error}")
                            warnings.append({
                                'source': 'semantic_scholar',
                                'message': f'Semantic Scholar unavailable and CrossRef fallback failed'
                            })
                    else:
                        # Not a timeout/rate limit - just log error
                        current_app.logger.error(f"Semantic Scholar search failed (non-timeout/rate-limit): {e}")

            if 'arxiv' in sources:
                try:
                    arxiv_result = ingestion_service.arxiv_connector.search_papers(
                        query, max_results=max_results
                    )
                    all_papers.extend(arxiv_result['papers'])
                except Exception as e:
                    current_app.logger.error(f"ArXiv search error: {e}")

            if 'crossref' in sources:
                try:
                    crossref_result = ingestion_service.crossref_connector.search_papers(
                        query, rows=max_results
                    )
                    all_papers.extend(crossref_result['papers'])
                except Exception as e:
                    current_app.logger.error(f"CrossRef search error: {e}")

            if 'google_scholar' in sources:
                # Google Scholar via SerpAPI with arXiv fallback on timeout
                current_app.logger.info(f"Google Scholar search requested. SerpAPI connector available: {ingestion_service.serpapi_connector is not None}")
                
                if not ingestion_service.serpapi_connector:
                    warnings.append({
                        'source': 'google_scholar',
                        'message': 'Google Scholar search skipped - SerpAPI key not configured. Get API key from https://serpapi.com/'
                    })
                    current_app.logger.warning("Google Scholar skipped - SerpAPI not configured")
                else:
                    try:
                        current_app.logger.info(f"Searching Google Scholar via SerpAPI for: {query}")
                        scholar_result = ingestion_service.serpapi_connector.search_papers(
                            query, limit=max_results
                        )
                        papers_count = len(scholar_result.get('papers', []))
                        all_papers.extend(scholar_result.get('papers', []))
                        current_app.logger.info(f"✓ Google Scholar returned {papers_count} papers")
                    except Exception as scholar_error:
                        error_msg = str(scholar_error).lower()
                        current_app.logger.error(f"Google Scholar search error (will check for timeout): {scholar_error}")

                        # Check if it's a timeout error
                        if 'timeout' in error_msg or 'timed out' in error_msg:
                            current_app.logger.warning(f"⚠️ Google Scholar search timed out: {scholar_error}")
                            current_app.logger.info(f"→ Falling back to arXiv for query: {query}")

                            try:
                                # Fallback to arXiv
                                arxiv_fallback_result = ingestion_service.arxiv_connector.search_papers(
                                    query, max_results=max_results
                                )
                                fallback_papers = arxiv_fallback_result.get('papers', [])
                                all_papers.extend(fallback_papers)
                                current_app.logger.info(f"✓ arXiv fallback returned {len(fallback_papers)} papers")

                                warnings.append({
                                    'source': 'google_scholar',
                                    'message': 'Google Scholar timed out - results from arXiv fallback'
                                })
                            except Exception as arxiv_error:
                                current_app.logger.error(f"arXiv fallback also failed: {arxiv_error}")
                                warnings.append({
                                    'source': 'google_scholar',
                                    'message': f'Google Scholar timed out and arXiv fallback failed: {str(arxiv_error)}'
                                })
                        else:
                            # Not a timeout - log error
                            current_app.logger.error(f"Google Scholar search failed (non-timeout): {scholar_error}")
                            warnings.append({
                                'source': 'google_scholar',
                                'message': f'Google Scholar search failed: {str(scholar_error)[:100]}'
                            })

            # Deduplicate
            deduplicated = ingestion_service._deduplicate_papers(all_papers)
            
            current_app.logger.info(f"Final search result: {len(all_papers)} papers before dedup, {len(deduplicated)} after dedup")

            response_data = {
                'total_count': len(deduplicated),
                'papers': deduplicated if deduplicated else [],
                'execution_time': 0  # Placeholder - can be enhanced with timing
            }

            if warnings:
                response_data['warnings'] = warnings
            
            current_app.logger.info(f"Returning response with {len(response_data.get('papers', []))} papers")

            return jsonify(response_data), 200

    except ValidationError as e:
        raise
    except Exception as e:
        current_app.logger.error(f"Search error: {e}")
        raise


@papers_bp.route('', methods=['GET'])
@optional_auth
def list_papers():
    """
    List papers from database with filtering and pagination.

    Query parameters:
        - page: Page number (default: 1)
        - per_page: Results per page (default: 20, max: 100)
        - tag: Filter by tag name
        - year: Filter by year
        - source: Filter by source (semantic_scholar, arxiv, crossref)
        - sort: Sort by (recent, citations, relevance) (default: recent)
        - q: Search query (title, abstract)

    Returns:
        {
            "total": int,
            "page": int,
            "per_page": int,
            "papers": List[dict]
        }
    """
    try:
        # Pagination
        page = request.args.get('page', 1, type=int)
        per_page = min(request.args.get('per_page', 20, type=int), 100)

        # Filters
        tag_name = request.args.get('tag')
        year = request.args.get('year', type=int)
        source = request.args.get('source')
        sort_by = request.args.get('sort', 'recent')
        search_query = request.args.get('q', '').strip()

        # Build query
        query = Paper.query

        # Filter by tag
        if tag_name:
            tag = Tag.query.filter_by(name=tag_name).first()
            if tag:
                query = query.join(PaperTag).filter(PaperTag.tag_id == tag.id)

        # Filter by year
        if year:
            query = query.filter(Paper.year == year)

        # Filter by source
        if source:
            query = query.filter(Paper.source == source)

        # Search in title and abstract
        if search_query:
            search_pattern = f'%{search_query}%'
            query = query.filter(
                or_(
                    Paper.title.ilike(search_pattern),
                    Paper.abstract.ilike(search_pattern)
                )
            )

        # Sorting
        if sort_by == 'recent':
            query = query.order_by(Paper.published_date.desc().nullslast())
        elif sort_by == 'citations':
            query = query.order_by(Paper.citation_count.desc())
        elif sort_by == 'relevance' and search_query:
            # Simple relevance: prioritize title matches
            query = query.order_by(
                Paper.title.ilike(search_pattern).desc(),
                Paper.citation_count.desc()
            )
        else:
            query = query.order_by(Paper.created_at.desc())

        # Paginate
        pagination = query.paginate(
            page=page,
            per_page=per_page,
            error_out=False
        )

        papers_data = [paper.to_dict() for paper in pagination.items]

        return jsonify({
            'total': pagination.total,
            'page': pagination.page,
            'per_page': pagination.per_page,
            'total_pages': pagination.pages,
            'papers': papers_data
        }), 200

    except Exception as e:
        current_app.logger.error(f"List papers error: {e}")
        raise


@papers_bp.route('/<int:paper_id>', methods=['GET'])
@optional_auth
def get_paper(paper_id):
    """
    Get detailed information about a specific paper.

    Path parameters:
        - paper_id: Paper ID

    Returns:
        {
            "id": int,
            "title": str,
            "abstract": str,
            "authors": List[str],
            "year": int,
            "tags": List[dict],
            "citations": List[dict],
            "references": List[dict],
            ...
        }
    """
    try:
        paper = Paper.query.filter_by(id=paper_id).first()

        if not paper:
            raise NotFoundError(f'Paper {paper_id} not found')

        # Get paper data
        paper_data = paper.to_dict()
        
        # For internal database papers, tags are already in the response
        # Optional: Fetch relationships if they exist
        try:
            paper_tags = db.session.query(PaperTag, Tag).join(Tag).filter(
                PaperTag.paper_id == paper_id
            ).all()
            if paper_tags:
                paper_data['tags'] = [tag.name for paper_tag, tag in paper_tags]
        except Exception:
            # If PaperTag relationship doesn't exist, use tags from paper.to_dict()
            pass

        return jsonify(paper_data), 200

    except NotFoundError as e:
        raise
    except Exception as e:
        current_app.logger.error(f"Get paper error: {e}")
        raise


@papers_bp.route('/<int:paper_id>/scores', methods=['GET'])
@optional_auth
def get_paper_scores(paper_id):
    """
    Get all scores for a paper.

    Path parameters:
        - paper_id: Paper ID

    Returns:
        {
            "paper_id": int,
            "tag_score": float,
            "citation_score": float,
            "novelty_score": float,
            "holmes_score": float,
            "is_diamond": bool,
            "scored_at": str (ISO 8601)
        }
    """
    try:
        paper = Paper.query.filter_by(id=paper_id).first()

        if not paper:
            raise NotFoundError(f'Paper {paper_id} not found')

        scores = {
            'paper_id': paper.id,
            'tag_score': float(paper.tag_score) if paper.tag_score else None,
            'citation_score': float(paper.citation_score) if paper.citation_score else None,
            'novelty_score': float(paper.novelty_score) if paper.novelty_score else None,
            'holmes_score': float(paper.holmes_score) if paper.holmes_score else None,
            'is_diamond': paper.is_diamond,
            'scored_at': paper.scored_at.isoformat() if paper.scored_at else None
        }

        return jsonify(scores), 200

    except NotFoundError as e:
        raise
    except Exception as e:
        current_app.logger.error(f"Get paper scores error: {e}")
        raise


@papers_bp.route('/<int:paper_id>/novel-combos', methods=['GET'])
@optional_auth
def get_paper_novel_combos(paper_id):
    """
    Get novel tag combinations for a paper.

    Path parameters:
        - paper_id: Paper ID

    Returns:
        {
            "paper_id": int,
            "novel_combos": [
                {
                    "tag_ids": [int, int],
                    "tag_names": [str, str],
                    "frequency": int
                }
            ]
        }
    """
    try:
        from ara_v2.services.tag_combo_tracker import TagComboTracker

        paper = Paper.query.filter_by(id=paper_id).first()

        if not paper:
            raise NotFoundError(f'Paper {paper_id} not found')

        tracker = TagComboTracker()
        novel_combos = tracker.get_paper_novel_combos(paper_id)

        return jsonify({
            'paper_id': paper_id,
            'novel_combos': novel_combos
        }), 200

    except NotFoundError as e:
        raise
    except Exception as e:
        current_app.logger.error(f"Get paper novel combos error: {e}")
        raise


@papers_bp.route('/<int:paper_id>/citations', methods=['POST', 'OPTIONS'])
@require_auth
@limiter.limit("10 per hour")
def build_citations(paper_id):
    """
    Build citation network for a paper (requires authentication).

    Path parameters:
        - paper_id: Paper ID

    Request body:
        {
            "max_citations": 50,  # Optional
            "max_references": 50   # Optional
        }

    Returns:
        {
            "citations_added": int,
            "references_added": int
        }
    """
    try:
        paper = Paper.query.filter_by(id=paper_id).first()

        if not paper:
            raise NotFoundError(f'Paper {paper_id} not found')

        data = request.get_json() or {}
        max_citations = data.get('max_citations', 50)
        max_references = data.get('max_references', 50)

        # Initialize ingestion service
        ingestion_service = PaperIngestionService()

        # Build citation network
        stats = ingestion_service.build_citation_network(
            paper,
            max_citations=max_citations,
            max_references=max_references
        )

        return jsonify(stats), 200

    except NotFoundError as e:
        raise
    except Exception as e:
        current_app.logger.error(f"Build citations error: {e}")
        raise


@papers_bp.route('/featured', methods=['GET'])
@optional_auth
def featured():
    """
    Get high-scoring papers (placeholder for Phase 2).

    Returns:
        {
            "papers": List[dict]
        }
    """
    # For now, return most cited papers
    papers = Paper.query.filter_by(deleted_at=None).order_by(
        Paper.citation_count.desc()
    ).limit(20).all()

    return jsonify({
        'papers': [paper.to_dict() for paper in papers]
    }), 200


@papers_bp.route('/diamonds', methods=['GET'])
@optional_auth
def diamonds():
    """
    Get diamond-classified papers (placeholder for Phase 2).

    Returns:
        {
            "papers": List[dict]
        }
    """
    # Placeholder: return papers with holmes_score when Phase 2 is implemented
    return jsonify({
        'message': 'Diamond classification coming in Phase 2',
        'papers': []
    }), 200


@papers_bp.route('/<int:paper_id>', methods=['DELETE', 'OPTIONS'])
@require_auth
def delete_paper(paper_id):
    """Delete a paper."""
    try:
        paper = Paper.query.filter_by(id=paper_id).first()
        if not paper:
            raise NotFoundError(f'Paper {paper_id} not found')

        # Remove PDF file if it exists
        if paper.pdf_path:
            full_path = os.path.join(current_app.root_path, '..', paper.pdf_path)
            if os.path.exists(full_path):
                try:
                    os.remove(full_path)
                except Exception as e:
                    current_app.logger.warning(f"Failed to remove PDF file {full_path}: {e}")

        db.session.delete(paper)
        db.session.commit()

        return jsonify({'message': 'Paper deleted successfully'}), 200

    except NotFoundError:
        raise
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Delete paper error: {e}")
        raise


@papers_bp.route('/upload', methods=['POST', 'OPTIONS'])
@require_auth
@limiter.limit("10 per hour")
def upload_paper():
    """Upload PDF paper from URL or file with automatic tagging."""
    filepath = None
    try:
        current_app.logger.info(f"UPLOAD ATTEMPT: Method={request.method}, Content-Type={request.headers.get('Content-Type')}")
        current_app.logger.info(f"UPLOAD USER: {g.get('current_user')}")
        
        upload_folder = os.path.join(current_app.root_path, '..', 'static', 'uploads')
        os.makedirs(upload_folder, exist_ok=True)
        
        # Check for force flag
        force_upload = request.args.get('force', '').lower() == 'true'

        # Determine if this is a URL-based or file-based upload
        is_json = request.is_json

        if is_json:
            # URL-based upload — store metadata + link only, no PDF download
            data = request.get_json()
            url = data.get('url', '').strip()

            if not url:
                raise ValidationError('No URL provided')

            filepath = None
            filename = None
            title = data.get('title', '').strip()
            authors = data.get('authors', '').strip() or 'Unknown'
            year = data.get('year')
            abstract = data.get('abstract', '').strip()
            tags = data.get('tags', [])
            external_url = url

        else:
            # File-based upload (backward compatibility)
            if 'file' not in request.files:
                raise ValidationError('No file or URL provided')

            file = request.files['file']
            if not file.filename or not allowed_file(file.filename):
                raise ValidationError('Only PDF files allowed')

            # Save file
            filename = secure_filename(file.filename)
            timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
            filename = f"{g.current_user.id}_{timestamp}_{filename}"
            filepath = os.path.join(upload_folder, filename)
            file.save(filepath)

            # Get metadata from form
            title = request.form.get('title', '').strip()
            authors = request.form.get('authors', '').strip() or 'Unknown'
            year = request.form.get('year', type=int)
            abstract = request.form.get('abstract', '').strip()
            tags = []
            external_url = None

        # Try to extract text from PDF (optional - not required)
        pdf_text = ""
        try:
            pdf_text = extract_pdf_text(filepath)
        except Exception:
            pass  # Ignore PDF extraction failures - metadata is what matters

        # Use extracted title if not provided
        if not title:
            title = filename.replace('.pdf', '')

        # Check duplicates
        duplicate = check_duplicate_by_title(title)
        if duplicate:
            if force_upload:
                # Delete existing duplicate
                current_app.logger.info(f"Overwriting duplicate paper: {duplicate.title} (ID: {duplicate.id})")
                
                # Delete old PDF to clean up
                if duplicate.pdf_path:
                    old_pdf_full = os.path.join(current_app.root_path, '..', duplicate.pdf_path)
                    if os.path.exists(old_pdf_full) and old_pdf_full != filepath:
                        try:
                            # Only delete if it's not the file we just uploaded (edge case)
                            os.remove(old_pdf_full)
                        except Exception as e:
                            current_app.logger.warning(f"Failed to delete old PDF: {e}")

                db.session.delete(duplicate)
                db.session.flush() # Ensure it's gone before insert
            else:
                if filepath and os.path.exists(filepath):
                    os.remove(filepath)
                raise ConflictError(f'Paper already exists: {duplicate.title}')

        # Create paper data
        timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
        paper_data = {
            'source': 'internal',
            'source_id': f'upload_{g.current_user.id}_{timestamp}',
            'title': title,
            'authors': authors,
            'year': year,
            'abstract': abstract,
            'pdf_path': f'static/uploads/{os.path.basename(filepath)}' if filepath else None,
            'pdf_text': pdf_text,
            'url': external_url or (f'/static/uploads/{os.path.basename(filepath)}' if filepath else None),
            'added_by': g.current_user.id
        }

        # Ingest with auto-tagging
        ingestion_service = PaperIngestionService()
        paper, is_new = ingestion_service.ingest_paper(paper_data, assign_tags=True)

        if not paper:
            if filepath and os.path.exists(filepath):
                os.remove(filepath)
            raise ARAError('Failed to save paper')

        # Add user-provided tags if any
        if tags and isinstance(tags, list):
            from ara_v2.services.tag_assigner import TagAssigner
            tag_assigner = TagAssigner()
            # Helper to assign tags manually
            valid_tags = [t.strip() for t in tags if t and isinstance(t, str)]
            if valid_tags:
                tag_objects = tag_assigner.get_or_create_tags(valid_tags)
                for tag in tag_objects:
                    # Check if already assigned
                    existing = PaperTag.query.filter_by(
                        paper_id=paper.id,
                        tag_id=tag.id
                    ).first()
                    
                    if not existing:
                        paper_tag = PaperTag(
                            paper_id=paper.id,
                            tag_id=tag.id,
                            confidence=1.0,  # User explicitly added this
                            is_novel_combo=False
                        )
                        db.session.add(paper_tag)

        db.session.commit()

        return jsonify({
            'success': True,
            'paper': paper.to_dict(),
            'message': 'Paper uploaded successfully'
        }), 201

    except (ValidationError, ConflictError):
        db.session.rollback()
        if filepath and os.path.exists(filepath):
            try:
                os.remove(filepath)
            except:
                pass
        raise
    except Exception as e:
        db.session.rollback()
        if filepath and os.path.exists(filepath):
            try:
                os.remove(filepath)
            except:
                pass
        current_app.logger.error(f"Upload error: {e}", exc_info=True)
        return jsonify({'error': f"SYSTEM ERROR: {str(e)}"}), 500
