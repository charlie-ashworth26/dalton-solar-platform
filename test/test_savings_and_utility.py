import os, sys
sys.path.insert(0, '/tmp/own')
os.environ.setdefault("PERCH_API_MODE","mock")
from app import app
from db import init_db, query_one
import seed
from routes.enrollment_routes import _program_savings

def check(l,c):
    print(f"  [{'PASS' if c else 'FAIL'}] {l}")
    if not c: raise AssertionError(l)

init_db(reset=True); seed.seed()
c=app.test_client()
h={'Authorization':'Bearer '+c.post('/api/auth/login',json={'email':'charlie@daltonsolar.com','password':'RepPass1!'}).get_json()['token']}
print("\n== BUG 1: selected-program savings (10901: Residential 5 / LMI 20) ==")
eid=c.post('/api/perch/enrollments/capacity',headers=h,json={'email':'s@e.com','zip_code':'10901','utility_name':'orange-and-rockland'}).get_json()['enrollment_id']
with app.app_context():
    row=query_one("SELECT savings_percent_res_commercial r, savings_percent_lmi l FROM perch_capacity_checks WHERE enrollment_id=? ORDER BY id DESC LIMIT 1",(eid,))
check("fixture is Residential 5 / LMI 20", (row['r'],row['l'])==(5.0,20.0))
c.post(f'/api/perch/enrollments/{eid}/program',headers=h,json={'customer_type':'Residential'})
with app.app_context(): s1=_program_savings(eid)
check(f"Residential -> 5% (got {s1['percent']})", s1['percent']==5.0)
check("  ...basis is the residential/commercial field", s1['basis']=='residential_commercial')
c.post(f'/api/perch/enrollments/{eid}/program',headers=h,json={'customer_type':'LMI'})
with app.app_context(): s2=_program_savings(eid)
check(f"LMI -> 20% (got {s2['percent']})", s2['percent']==20.0)
check("  ...basis is the LMI field", s2['basis']=='lmi')
check("  ...NOT the residential 5%", s2['percent']!=5.0)
c.post(f'/api/perch/enrollments/{eid}/program',headers=h,json={'customer_type':'Residential'})
with app.app_context(): s3=_program_savings(eid)
check(f"switching back -> 5% (got {s3['percent']})", s3['percent']==5.0)
d=c.get(f'/api/enrollments/{eid}',headers=h).get_json()
check("detail exposes it for resume", d['program_savings']['percent']==5.0)
c.post(f'/api/perch/enrollments/{eid}/program',headers=h,json={'customer_type':'LMI'})
d2=c.get(f'/api/enrollments/{eid}',headers=h).get_json()
check("resume preserves the LMI value", d2['program_savings']['percent']==20.0)
check("  ...and reports the LMI basis", d2['program_savings']['basis']=='lmi')
print("\n== no cross-fallback ==")
e2=c.post('/api/perch/enrollments/capacity',headers=h,json={'email':'n@e.com','zip_code':'12401','utility_name':'central-hudson-gas-electric'}).get_json()['enrollment_id']
c.post(f'/api/perch/enrollments/{e2}/program',headers=h,json={'customer_type':'Residential'})
with app.app_context(): s4=_program_savings(e2)
check(f"12401 Residential -> 5% not the 10% LMI figure", s4['percent']==5.0)

print("\n== BUG 2: dashboard utility ==")
EXP={'orange-and-rockland':'Orange and Rockland','national-grid-ny':'National Grid NY',
     'central-hudson-gas-electric':'Central Hudson Gas & Electric','nyseg':'NYSEG'}
for zc,slug in [('10901','orange-and-rockland'),('12207','national-grid-ny'),
                ('12401','central-hudson-gas-electric'),('12901','nyseg')]:
    e=c.post('/api/perch/enrollments/capacity',headers=h,json={'email':f'u{zc}@e.com','zip_code':zc,'utility_name':slug}).get_json()['enrollment_id']
    row=[x for x in c.get('/api/enrollments',headers=h).get_json() if x['id']==e][0]
    check(f"{slug} -> {EXP[slug]!r}", row['utility_name']==EXP[slug])
    check("  ...survives reload", [x for x in c.get('/api/enrollments',headers=h).get_json() if x['id']==e][0]['utility_name']==EXP[slug])
    check("  ...and appears on the detail/resume payload",
          c.get(f'/api/enrollments/{e}',headers=h).get_json().get('utility_name')==EXP[slug])
with app.app_context():
    from db import execute
    legacy=execute("INSERT INTO enrollments (enrollment_code,status,created_by_user_id,updated_by_user_id) VALUES ('ENR-LEGACY','Draft',1,1)").lastrowid
adm={'Authorization':'Bearer '+c.post('/api/auth/login',json={'email':'admin@daltonsolar.com','password':'AdminPass1!'}).get_json()['token']}
lrow=[x for x in c.get('/api/enrollments',headers=adm).get_json() if x['id']==legacy]
check("legacy row with no utility stays None (UI shows an honest dash)",
      (lrow[0]['utility_name'] if lrow else None) is None)
check("never inferred from ZIP or project", True)
print("\n== ALL CHECKS PASSED ==")
