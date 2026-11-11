"""Read-only audit before uniqueness migration. Never guesses which paid record to delete."""
from __future__ import annotations
import argparse
import json
import sqlite3
from pathlib import Path


def audit(path):
    with sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        queries = {
            'duplicate_open_plate': "SELECT plate,COUNT(*) AS count,GROUP_CONCAT(id) AS ids FROM parking_record WHERE status='in' GROUP BY plate HAVING COUNT(*)>1",
            'duplicate_open_space': "SELECT space_no,COUNT(*) AS count,GROUP_CONCAT(id) AS ids FROM parking_record WHERE status='in' AND space_no IS NOT NULL GROUP BY space_no HAVING COUNT(*)>1",
            'duplicate_occupied_plate': "SELECT current_plate,COUNT(*) AS count FROM parking_space WHERE status='occupied' AND current_plate IS NOT NULL GROUP BY current_plate HAVING COUNT(*)>1",
            'occupancy_mismatch': "SELECT p.id,p.plate,p.space_no FROM parking_record p LEFT JOIN parking_space s ON s.space_no=p.space_no WHERE p.status='in' AND p.space_no IS NOT NULL AND (s.status IS NULL OR s.status!='occupied' OR s.current_plate IS NULL OR s.current_plate!=p.plate)",
            'orphan_occupied': "SELECT s.space_no,s.current_plate FROM parking_space s LEFT JOIN parking_record p ON p.space_no=s.space_no AND p.plate=s.current_plate AND p.status='in' WHERE s.status='occupied' AND p.id IS NULL",
            'duplicate_payment': 'SELECT parking_record_id,COUNT(*) AS count FROM payment_record GROUP BY parking_record_id HAVING COUNT(*)>1',
        }
        findings = {name:[dict(r) for r in db.execute(sql)] for name,sql in queries.items()}
        return {'read_only':True,'consistent':not any(findings.values()),'findings':findings,
                'resolution':'Back up with SQLite backup API. Reconcile listed record IDs against payment/entry evidence; resolve through audited manual exit, never delete payment history. Rerun audit before migration.'}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('database');p.add_argument('--out',type=Path);p.add_argument('--dry-run',action='store_true',default=True)
    args=p.parse_args();result=audit(args.database)
    output=json.dumps(result,ensure_ascii=False,indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True,exist_ok=True);args.out.write_text(output,encoding='utf-8')
    print(output)
    raise SystemExit(0 if result['consistent'] else 2)
