import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_resume_upload_targets_only_the_real_resume_control():
    script = r"""
const logic = await import('./browser-extension/application-fields.mjs');
const cases = {
  requiredResume: logic.resumeFieldsNeedingUpload([
    {field_id:'duplicate', type:'file', required:false, value:''},
    {field_id:'resume', type:'file', label:'Resume', required:true, value:''},
    {field_id:'cover', type:'file', label:'Cover Letter', required:false, value:''},
  ]).map(field => field.field_id),
  acceptedResume: logic.resumeFieldsNeedingUpload([
    {field_id:'duplicate', type:'file', required:false, value:''},
    {field_id:'resume', type:'file', label:'Resume', required:true, value:'tailored.pdf'},
  ]).map(field => field.field_id),
  coverOnly: logic.resumeFieldsNeedingUpload([
    {field_id:'cover', type:'file', label:'Cover Letter', required:true, value:''},
  ]).map(field => field.field_id),
  genericOnly: logic.resumeFieldsNeedingUpload([
    {field_id:'upload', type:'file', required:false, value:''},
  ]).map(field => field.field_id),
  accepted: logic.resumeFileAccepted([
    {field_id:'resume', type:'file', label:'CV', required:false, value:'victor.pdf'},
    {field_id:'cover', type:'file', label:'Cover Letter', required:false, value:''},
  ]),
};
process.stdout.write(JSON.stringify(cases));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script], cwd=ROOT, check=True, capture_output=True, text=True
    )
    assert json.loads(result.stdout) == {
        "requiredResume": ["resume"],
        "acceptedResume": [],
        "coverOnly": [],
        "genericOnly": ["upload"],
        "accepted": True,
    }
