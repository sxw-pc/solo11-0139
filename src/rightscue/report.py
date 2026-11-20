"""Recomputable settlement evidence view."""
def build(db):
    tables=['show_slot','program_revision','cue_evidence','work_match','rights_claim','royalty_case','royalty_event','decision',
            'master_contract','master_segment','master_block','master_signature','master_revocation','master_certificate']
    return {table:[dict(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY 1')] for table in tables}
