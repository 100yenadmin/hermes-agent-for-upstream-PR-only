"""Recount the frozen, explicitly curated historical sample; no network or reclassification."""
import json
from pathlib import Path

sample = json.loads(Path(__file__).with_name('hermes-first-wave.json').read_text())
opened = [row for row in sample['clusters'] if row['historical_disposition'] in
          ('open_report_evidenced', 'pre_window_carry_in')]
merged = [row for row in sample['clusters'] if row['historical_disposition'] == 'merged_qualifying']
fixes = {pr['number'] for row in merged for pr in row['audited_fix_pull_requests']}
print(json.dumps({'selected_clusters': len(sample['clusters']), 'open_report_clusters': len(opened),
                  'qualifying_merged_clusters': len(merged), 'qualifying_fix_prs': len(fixes)}, sort_keys=True))
