import json
import unittest

from scripts.process_orvix_csv import (
    EVENT_FIELDS,
    OUTPUT_FIELDS,
    ProcessingError,
    output_row,
    parse_model_response,
    validate_response,
)


RAW_TEXT = 'KRL Bekasi line mengalami gangguan sinyal sejak pukul 08.00.'


def event(index=1, quote='gangguan sinyal', **updates):
    value = {
        'event_index': index,
        'event_status': 'potential_disruption',
        'disruption_type': 'signal_or_power_issue',
        'disruption_type_evidence': 'explicit',
        'affected_entities': [{
            'entity_type': 'line',
            'value': 'Bekasi line',
            'evidence_type': 'explicit',
            'evidence_quote': 'Bekasi line',
        }],
        'severity': 'unknown',
        'severity_evidence': 'unknown',
        'event_start_time': None,
        'event_start_time_evidence': 'unknown',
        'event_end_time': None,
        'event_end_time_evidence': 'unknown',
        'uncertainty_status': 'yes',
        'uncertainty_note': 'End time is not stated.',
        'explanation': 'The post states that the signal has a disruption.',
        'evidence_quotes': [quote],
    }
    value.update(updates)
    return value


def payload(*events):
    return {'events': list(events)}


class OrvixCsvProcessorTests(unittest.TestCase):
    def test_valid_event_maps_to_pending_output_row(self):
        validated = validate_response(payload(event()), RAW_TEXT)
        row = output_row('snapshot-1', validated[0])

        self.assertEqual(tuple(row), OUTPUT_FIELDS)
        self.assertEqual(row['snapshot_id'], 'snapshot-1')
        self.assertEqual(row['event_index'], 1)
        self.assertEqual(row['review_status'], 'pending')
        self.assertEqual(row['review_notes'], '')
        self.assertEqual(json.loads(row['evidence_quotes_json']), ['gangguan sinyal'])

    def test_single_disruption_event_uses_index_one(self):
        validated = validate_response(payload(event()), RAW_TEXT)

        self.assertEqual(len(validated), 1)
        self.assertEqual(validated[0]['event_index'], 1)
        self.assertEqual(validated[0]['event_status'], 'potential_disruption')

    def test_multiple_distinct_events_preserve_contiguous_indices(self):
        second = event(
            index=2,
            quote='terlambat',
            disruption_type='delay',
            affected_entities=[{
                'entity_type': 'station', 'value': 'Bekasi', 'evidence_type': 'explicit',
                'evidence_quote': 'Bekasi',
            }],
            evidence_quotes=['terlambat'],
        )

        validated = validate_response(payload(event(), second), RAW_TEXT + ' Kereta terlambat di Bekasi.')

        self.assertEqual([item['event_index'] for item in validated], [1, 2])

    def test_negative_example_keeps_one_no_disruption_row(self):
        negative = event(
            index=0,
            event_status='no_disruption',
            disruption_type='not_applicable',
            disruption_type_evidence='not_applicable',
            affected_entities=[],
            severity='not_applicable',
            severity_evidence='not_applicable',
            event_start_time=None,
            event_start_time_evidence='not_applicable',
            event_end_time=None,
            event_end_time_evidence='not_applicable',
            uncertainty_status='no',
            uncertainty_note='',
            evidence_quotes=[],
        )

        validated = validate_response(payload(negative), 'KRL beroperasi normal tanpa gangguan.')

        self.assertEqual(len(validated), 1)
        self.assertEqual(validated[0]['event_index'], 0)
        self.assertEqual(validated[0]['event_status'], 'no_disruption')

    def test_whole_post_unclear_uses_one_index_zero_row(self):
        unclear = event(
            index=0,
            event_status='unclear',
            disruption_type='unknown',
            disruption_type_evidence='unknown',
            affected_entities=[],
            severity='unknown',
            severity_evidence='unknown',
            event_start_time=None,
            event_start_time_evidence='unknown',
            event_end_time=None,
            event_end_time_evidence='unknown',
            uncertainty_status='yes',
            uncertainty_note='The post does not provide enough information.',
        )

        validated = validate_response(payload(unclear), RAW_TEXT)

        self.assertEqual(len(validated), 1)
        self.assertEqual(validated[0]['event_index'], 0)
        self.assertEqual(validated[0]['event_status'], 'unclear')

    def test_malformed_json_and_invalid_enum_are_rejected(self):
        with self.assertRaises(ProcessingError):
            parse_model_response('{not json', RAW_TEXT)
        with self.assertRaises(ProcessingError):
            validate_response(payload(event(disruption_type='made_up')), RAW_TEXT)

    def test_invalid_event_indices_are_rejected(self):
        with self.assertRaises(ProcessingError):
            validate_response(payload(event(index=0)), RAW_TEXT)
        with self.assertRaises(ProcessingError):
            validate_response(payload(event(index=1), event(index=3)), RAW_TEXT)
        with self.assertRaises(ProcessingError):
            validate_response(payload(event(index=1), event(index=1)), RAW_TEXT)
        with self.assertRaises(ProcessingError):
            validate_response(payload(event(index=1), event(index=3)), RAW_TEXT)
        missing_index = event(index=2)
        del missing_index['event_index']
        with self.assertRaises(ProcessingError):
            validate_response(payload(event(index=1), missing_index), RAW_TEXT)

    def test_sentinel_row_cannot_be_mixed_with_disruption_events(self):
        negative = event(
            index=0,
            event_status='no_disruption',
            disruption_type='not_applicable',
            disruption_type_evidence='not_applicable',
            affected_entities=[],
            severity='not_applicable',
            severity_evidence='not_applicable',
            event_start_time=None,
            event_start_time_evidence='not_applicable',
            event_end_time=None,
            event_end_time_evidence='not_applicable',
            uncertainty_status='no',
            uncertainty_note='',
            evidence_quotes=[],
        )

        with self.assertRaises(ProcessingError):
            validate_response(payload(negative, event(index=1)), RAW_TEXT)

    def test_unsupported_evidence_quote_is_rejected(self):
        with self.assertRaises(ProcessingError):
            validate_response(payload(event(quote='line is closed')), RAW_TEXT)


if __name__ == '__main__':
    unittest.main()
