"""
Tag endpoints for ARA v2.
Handles tag retrieval and statistics.
"""

from flask import Blueprint, request, jsonify, current_app
from ara_v2.models.tag import Tag, TagCombo
from ara_v2.models.paper_tag import PaperTag
from ara_v2.models.paper import Paper
from ara_v2.middleware.auth import optional_auth
from ara_v2.utils.database import db
from ara_v2.utils.errors import NotFoundError
from sqlalchemy import func, desc

tags_bp = Blueprint('tags', __name__)


@tags_bp.route('/', methods=['GET'])
@optional_auth
def list_tags():
    """
    Get all tags with statistics.

    Query parameters:
        - category: Filter by category (optional)
        - min_papers: Minimum paper count (optional)
        - sort: Sort by (name, papers, recent) (default: papers)
        - limit: Maximum number of tags to return (default: all)

    Returns:
        {
            "total": int,
            "tags": List[dict]
        }
    """
    try:
        # Filters
        category = request.args.get('category')
        min_papers = request.args.get('min_papers', type=int, default=0)
        sort_by = request.args.get('sort', 'papers')
        limit = request.args.get('limit', type=int)

        # Build query
        query = Tag.query

        # Filter by category
        if category:
            query = query.filter(Tag.category == category)

        # Filter by minimum papers
        if min_papers > 0:
            query = query.filter(Tag.paper_count >= min_papers)

        # Sorting
        if sort_by == 'name':
            query = query.order_by(Tag.name.asc())
        elif sort_by == 'recent':
            query = query.order_by(Tag.last_used.desc().nullslast())
        else:  # papers (default)
            query = query.order_by(Tag.paper_count.desc())

        # Apply limit
        if limit:
            query = query.limit(limit)

        tags = query.all()

        return jsonify({
            'total': len(tags),
            'tags': [tag.to_dict() for tag in tags]
        }), 200

    except Exception as e:
        current_app.logger.error(f"List tags error: {e}")
        raise


@tags_bp.route('/<string:tag_slug>', methods=['GET'])
@optional_auth
def get_tag(tag_slug):
    """
    Get detailed information about a specific tag.

    Path parameters:
        - tag_slug: Tag slug

    Returns:
        {
            "id": int,
            "name": str,
            "slug": str,
            "category": str,
            "paper_count": int,
            "description": str,
            "related_tags": List[dict],
            "recent_papers": List[dict]
        }
    """
    try:
        tag = Tag.query.filter_by(slug=tag_slug).first()

        if not tag:
            raise NotFoundError(f'Tag "{tag_slug}" not found')

        tag_data = tag.to_dict()

        # Get related tags (tags that frequently appear together)
        related_tags = db.session.query(
            Tag,
            func.count(PaperTag.paper_id).label('co_occurrence')
        ).join(
            PaperTag, PaperTag.tag_id == Tag.id
        ).filter(
            PaperTag.paper_id.in_(
                db.session.query(PaperTag.paper_id).filter(PaperTag.tag_id == tag.id)
            ),
            Tag.id != tag.id
        ).group_by(Tag.id).order_by(
            desc('co_occurrence')
        ).limit(10).all()

        tag_data['related_tags'] = [
            {
                'id': related_tag.id,
                'name': related_tag.name,
                'slug': related_tag.slug,
                'co_occurrence_count': count
            }
            for related_tag, count in related_tags
        ]

        # Get recent papers with this tag
        recent_papers = db.session.query(Paper).join(PaperTag).filter(
            PaperTag.tag_id == tag.id).order_by(
            Paper.created_at.desc().nullslast()
        ).limit(10).all()

        tag_data['recent_papers'] = [
            {
                'id': paper.id,
                'title': paper.title,
                'year': paper.year,
                'authors': paper.authors,
                'citation_count': paper.citation_count
            }
            for paper in recent_papers
        ]

        return jsonify(tag_data), 200

    except NotFoundError as e:
        raise
    except Exception as e:
        current_app.logger.error(f"Get tag error: {e}")
        raise


@tags_bp.route('/trending', methods=['GET'])
@optional_auth
def trending_tags():
    """
    Get trending/fastest-growing tags.

    Query parameters:
        - limit: Maximum number of tags (default: 20)
        - min_frequency: Minimum tag frequency (default: 2)

    Returns:
        {
            "tags": List[dict with growth_rate]
        }
    """
    try:
        limit = request.args.get('limit', 20, type=int)
        min_frequency = request.args.get('min_frequency', 2, type=int)

        # Get tags ordered by growth rate (papers per month)
        tags = Tag.query.filter(
            Tag.frequency >= min_frequency,
            Tag.growth_rate != None
        ).order_by(
            Tag.growth_rate.desc()
        ).limit(limit).all()

        result = []
        for tag in tags:
            tag_dict = tag.to_dict()
            # Add growth rate to response
            tag_dict['growth_rate'] = float(tag.growth_rate) if tag.growth_rate else 0.0
            result.append(tag_dict)

        return jsonify({
            'total': len(result),
            'tags': result
        }), 200

    except Exception as e:
        current_app.logger.error(f"Trending tags error: {e}")
        raise


@tags_bp.route('/combos', methods=['GET'])
@optional_auth
def tag_combos():
    """
    Get interesting tag combinations.

    Query parameters:
        - min_frequency: Minimum occurrence count (default: 1)
        - novel_only: Only show novel combos (frequency <= 3) (default: false)
        - limit: Maximum number of combos (default: 50)

    Returns:
        {
            "combos": List[dict]
        }
    """
    try:
        from ara_v2.services.tag_combo_tracker import TagComboTracker

        min_frequency = request.args.get('min_frequency', 1, type=int)
        novel_only = request.args.get('novel_only', 'false').lower() == 'true'
        limit = request.args.get('limit', 50, type=int)

        tracker = TagComboTracker()

        if novel_only:
            # Get novel combinations
            result = tracker.get_novel_combinations(limit=limit, min_frequency=min_frequency)
        else:
            # Get popular combinations
            result = tracker.get_popular_combinations(limit=limit, min_frequency=min_frequency)

        return jsonify({
            'total': len(result),
            'combos': result,
            'novel_only': novel_only
        }), 200

    except Exception as e:
        current_app.logger.error(f"Tag combos error: {e}")
        raise


@tags_bp.route('/categories', methods=['GET'])
@optional_auth
def tag_categories():
    """
    Get all tag categories with counts.

    Returns:
        {
            "categories": List[dict]
        }
    """
    try:
        # Get unique categories with counts
        categories = db.session.query(
            Tag.category,
            func.count(Tag.id).label('tag_count'),
            func.sum(Tag.paper_count).label('total_papers')
        ).group_by(
            Tag.category
        ).order_by(
            desc('tag_count')
        ).all()

        return jsonify({
            'categories': [
                {
                    'name': category,
                    'tag_count': tag_count,
                    'paper_count': total_papers or 0
                }
                for category, tag_count, total_papers in categories
            ]
        }), 200

    except Exception as e:
        current_app.logger.error(f"Tag categories error: {e}")
        raise


@tags_bp.route('/search', methods=['GET'])
@optional_auth
def search_tags():
    """
    Search for tags by name or description.

    Query parameters:
        - q: Search query (required)
        - limit: Maximum number of results (default: 20)

    Returns:
        {
            "total": int,
            "tags": List[dict]
        }
    """
    try:
        query_str = request.args.get('q', '').strip()
        limit = request.args.get('limit', 20, type=int)

        if not query_str:
            return jsonify({
                'total': 0,
                'tags': []
            }), 200

        # Search in name and description
        search_pattern = f'%{query_str}%'
        tags = Tag.query.filter(
            db.or_(
                Tag.name.ilike(search_pattern),
                Tag.description.ilike(search_pattern)
            )
        ).order_by(
            Tag.paper_count.desc()
        ).limit(limit).all()

        return jsonify({
            'total': len(tags),
            'tags': [tag.to_dict() for tag in tags]
        }), 200

    except Exception as e:
        current_app.logger.error(f"Search tags error: {e}")
        raise
