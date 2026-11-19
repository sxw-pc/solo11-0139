from pathlib import Path

from rightscue import report, show, sightline
from rightscue.store import connect, read

ROOT = Path(__file__).resolve().parents[1] / 'fixtures'


def test_manual_seat_survey_and_replay(tmp_path):
    db = connect(tmp_path / 'seat.sqlite')
    with db:
        show.run(db, read(ROOT / 'show-ok.json'))
        result = sightline.run(db, read(ROOT / 'sightline-ok.json'))
        assert [r['status'] for r in result['seats']] == ['clear', 'obstructed']
        assert sightline.run(db, read(ROOT / 'sightline-ok.json'))['status'] == 'duplicate'
    assert len(report.build(db)['seat_survey']) == 2


def test_survey_requires_show(tmp_path):
    db = connect(tmp_path / 'seat.sqlite')
    try:
        with db:
            sightline.run(db, read(ROOT / 'sightline-ok.json'))
    except ValueError as exc:
        assert 'show not found' in str(exc)
    else:
        raise AssertionError('unknown show accepted')
