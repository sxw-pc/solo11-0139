"""Receive a venue's manual visual-clear survey for individual seats."""

from .store import decide, ingest


def run(db, data):
    state = ingest(db, 'sightline', data)
    if state != 'new':
        return {'source_id': data['source_id'], 'status': state}
    show_id = data['show_id']
    if not db.execute('SELECT 1 FROM show_slot WHERE id=?', (show_id,)).fetchone():
        raise ValueError('show not found')
    result = []
    for row in data['seats']:
        clear = bool(row['visual_clear'])
        db.execute('INSERT INTO seat_survey VALUES(?,?,?,?,?)',
                   (row['id'], show_id, row['seat_id'], int(clear), data['source_id']))
        status = 'clear' if clear else 'obstructed'
        db.execute('INSERT INTO seat_case VALUES(?,?,?) ON CONFLICT(show_id,seat_id) '
                   'DO UPDATE SET status=excluded.status', (show_id, row['seat_id'], status))
        decide(db, 'sightline', f'{show_id}:{row["seat_id"]}', status,
               'manual visual-clear survey', [data['source_id'], row['id']])
        result.append({'seat_id': row['seat_id'], 'status': status})
    return {'show_id': show_id, 'seats': result}
