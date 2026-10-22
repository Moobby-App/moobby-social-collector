"""Resolve extracted KRL station mentions against the private station reference.

This module has no Orvix or worker dependency. Exact normalized canonical-name
and curated-alias matches are accepted; fuzzy matches are never made.
"""

from decimal import Decimal, InvalidOperation
import re
import unicodedata


REFERENCE_QUERY = """
SELECT station_id, source_station_code, canonical_name, aliases, latitude, longitude
FROM reference.krl_stations
ORDER BY station_id
"""
ENTITY_FIELDS = {'entity_type', 'value', 'evidence_type', 'evidence_quote'}
RESOLUTION_STATUSES = {'resolved', 'ambiguous', 'unresolved'}
_SPACE_RE = re.compile(r'\s+')


class StationResolutionError(ValueError):
    """Invalid entity or malformed station reference data."""


def normalize_station_name(value):
    """Normalize orthography and punctuation while retaining exact matching.

    Case, Unicode compatibility forms, punctuation, repeated whitespace, and
    an optional Indonesian "Stasiun" prefix are normalized. Spelling changes,
    abbreviations, and phonetic/fuzzy matches are not inferred.
    """
    if not isinstance(value, str):
        raise StationResolutionError('Station name must be text.')
    text = unicodedata.normalize('NFKC', value).casefold().strip()
    normalized_chars = []
    for char in text:
        category = unicodedata.category(char)
        normalized_chars.append(' ' if category.startswith(('P', 'S')) else char)
    text = _SPACE_RE.sub(' ', ''.join(normalized_chars)).strip()
    if text.startswith('stasiun '):
        text = text[len('stasiun '):].strip()
    return text


def _station_record(record):
    keys = ('station_id', 'source_station_code', 'canonical_name', 'aliases', 'latitude', 'longitude')
    if isinstance(record, dict):
        station = {key: record.get(key) for key in keys}
    else:
        try:
            station = dict(zip(keys, record))
        except TypeError as error:
            raise StationResolutionError('Station reference record has an invalid format.') from error
        if len(station) != len(keys):
            raise StationResolutionError('Station reference record has an invalid format.')

    for field in ('station_id', 'source_station_code', 'canonical_name'):
        if not isinstance(station[field], str) or not station[field].strip():
            raise StationResolutionError('Station reference contains an empty identifier or name.')
        station[field] = station[field].strip()
    if not station['station_id'].startswith('open-krl:'):
        raise StationResolutionError('Station reference ID is not source-qualified.')
    aliases = station['aliases']
    if aliases is None:
        aliases = []
    if not isinstance(aliases, (list, tuple)) or any(not isinstance(alias, str) or not alias.strip() for alias in aliases):
        raise StationResolutionError('Station aliases must be nonempty text values.')
    station['aliases'] = [alias.strip() for alias in aliases]
    try:
        latitude = Decimal(str(station['latitude']))
        longitude = Decimal(str(station['longitude']))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise StationResolutionError('Station reference contains invalid coordinates.') from error
    if (not latitude.is_finite() or not longitude.is_finite()
            or not Decimal(-90) <= latitude <= Decimal(90)
            or not Decimal(-180) <= longitude <= Decimal(180)):
        raise StationResolutionError('Station reference contains out-of-range coordinates.')
    station['latitude'] = float(latitude)
    station['longitude'] = float(longitude)
    return station


class KrlStationResolver:
    """In-memory exact resolver over rows from reference.krl_stations."""

    def __init__(self, station_records):
        self._stations = {}
        self._name_index = {}
        for record in station_records:
            station = _station_record(record)
            station_id = station['station_id']
            if station_id in self._stations:
                raise StationResolutionError('Station reference contains duplicate station IDs.')
            self._stations[station_id] = station
            self._add_name(station['canonical_name'], station_id, 'canonical')
            for alias in station['aliases']:
                self._add_name(alias, station_id, 'alias')

    def _add_name(self, name, station_id, match_type):
        normalized = normalize_station_name(name)
        if not normalized:
            return
        entry = self._name_index.setdefault(normalized, {})
        # Prefer canonical over alias for one station if the same spelling is
        # present in both places; multiple station IDs remain ambiguous.
        current = entry.get(station_id)
        if current != 'canonical':
            entry[station_id] = match_type

    def resolve_entity(self, entity):
        """Resolve one processor entity, preserving its original text/evidence."""
        if not isinstance(entity, dict) or set(entity) != ENTITY_FIELDS:
            raise StationResolutionError('Extracted entity does not match the processor contract.')
        entity_type = entity['entity_type']
        value = entity['value']
        evidence_type = entity['evidence_type']
        evidence_quote = entity['evidence_quote']
        if not isinstance(entity_type, str) or entity_type not in {'station', 'line', 'route', 'other'}:
            raise StationResolutionError('Extracted entity has an unsupported entity type.')
        if not isinstance(value, str) or not value.strip():
            raise StationResolutionError('Extracted entity value is empty.')
        if not isinstance(evidence_type, str) or evidence_type not in {'explicit', 'inferred'}:
            raise StationResolutionError('Extracted entity evidence type is invalid.')
        if not isinstance(evidence_quote, str) or not evidence_quote:
            raise StationResolutionError('Extracted entity evidence quote is empty.')

        result = {
            'entity_type': entity_type,
            'value': value,
            'evidence_type': evidence_type,
            'evidence_quote': evidence_quote,
            'resolution_status': 'unresolved',
            'resolution_reason': None,
        }
        if entity_type != 'station':
            result['resolution_reason'] = 'non_station_entity'
            return result

        matches = self._name_index.get(normalize_station_name(value), {})
        if not matches:
            result['resolution_reason'] = 'no_exact_match'
            return result
        if len(matches) > 1:
            result['resolution_status'] = 'ambiguous'
            result['resolution_reason'] = 'multiple_exact_matches'
            result['candidate_count'] = len(matches)
            return result

        station_id, match_type = next(iter(matches.items()))
        station = self._stations[station_id]
        result.update(
            resolution_status='resolved',
            resolution_reason='exact_' + match_type + '_match',
            station_id=station['station_id'],
            source_station_code=station['source_station_code'],
            canonical_name=station['canonical_name'],
            latitude=station['latitude'],
            longitude=station['longitude'],
            location_semantics='mentioned_station_only',
        )
        return result

    def resolve_affected_entities(self, entities):
        """Resolve every entity independently; never choose among mentions."""
        if not isinstance(entities, list):
            raise StationResolutionError('affected_entities must be a JSON array.')
        return [self.resolve_entity(entity) for entity in entities]


def load_reference_stations(db_params, connect=None):
    """Read station rows in a read-only transaction and return a resolver.

    ``db_params`` should come from a private, read-only database login. The
    transaction itself is explicitly marked read-only before any SELECT.
    ``connect`` is injectable for offline tests.
    """
    if connect is None:
        try:
            import psycopg
        except ImportError as error:
            raise StationResolutionError('Install the repository PostgreSQL dependency first.') from error
        connect = psycopg.connect
    try:
        with connect(**db_params, prepare_threshold=None) as connection:
            connection.execute('SET TRANSACTION READ ONLY')
            connection.execute("SET LOCAL statement_timeout = '10s'")
            rows = connection.execute(REFERENCE_QUERY).fetchall()
        return KrlStationResolver(rows)
    except StationResolutionError:
        raise
    except Exception as error:
        sqlstate = getattr(error, 'sqlstate', None)
        suffix = ' (SQLSTATE {})'.format(sqlstate) if isinstance(sqlstate, str) and len(sqlstate) == 5 else ''
        raise StationResolutionError(
            'Could not read the station reference: {}{}'.format(type(error).__name__, suffix)
        ) from error
