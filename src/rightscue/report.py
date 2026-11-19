"""Recomputable settlement evidence view."""
def build(db):
    tables=['show_slot','program_revision','cue_evidence','work_match','rights_claim','royalty_case','royalty_event','audio_probe','audio_health','decision']
    return {table:[dict(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY 1')] for table in tables}
