"""Control panel for the fake supplier: open /__admin/ui in a browser."""
import main
from collections import deque
from datetime import datetime, timezone
from fastapi import Request
from fastapi.responses import HTMLResponse

api = main.app.inner  # the FastAPI app inside the slash-collapsing wrapper
app = main.app
ONLY_LISTED = {"on": True}  # True = hotels not in the panel get no rooms
_availability = main.availability


SEARCHES = deque(maxlen=50)


def availability_only_listed(request, only=None):
    out = _availability(request, only)
    asked = [p["property_id"] for p in out]
    if ONLY_LISTED["on"]:
        out = [p for p in out if p["property_id"] in main.PROPERTIES]
    q = request.query_params
    SEARCHES.append({"time": datetime.now(timezone.utc).strftime("%H:%M:%S"), "checkin": q.get("checkin"),
                     "currency": q.get("currency"), "asked": len(asked),
                     "yours": [i for i in asked if i in main.PROPERTIES], "returned": len(out)})
    return out


main.availability = availability_only_listed


def settings():
    return {"tax_percent": main.TAX_PCT, "price_change_percent": main.PRICE_CHANGE_PCT,
            "default_scenario": main.DEFAULT_SCENARIO, "timeout_seconds": main.TIMEOUT_SLEEP,
            "slow_seconds": main.SLOW_SLEEP, "nrf_cancel": main.NRF_CANCEL,
            "only_listed": ONLY_LISTED["on"]}


@api.get("/__admin/settings")
async def get_settings():
    return settings()


@api.post("/__admin/settings")
async def set_settings(request: Request):
    b = await request.json()
    if "tax_percent" in b: main.TAX_PCT = float(b["tax_percent"])
    if "price_change_percent" in b: main.PRICE_CHANGE_PCT = float(b["price_change_percent"])
    if b.get("default_scenario") in main.SCENARIOS: main.DEFAULT_SCENARIO = b["default_scenario"]
    if "timeout_seconds" in b: main.TIMEOUT_SLEEP = float(b["timeout_seconds"])
    if "slow_seconds" in b: main.SLOW_SLEEP = float(b["slow_seconds"])
    if b.get("nrf_cancel") in ("refuse", "allow"): main.NRF_CANCEL = b["nrf_cancel"]
    if "only_listed" in b: ONLY_LISTED["on"] = bool(b["only_listed"])
    return settings()


@api.get("/__admin/searches")
async def searches():
    return list(SEARCHES)[::-1]


@api.delete("/__admin/properties/{pid}")
async def remove_property(pid: str):
    main.PROPERTIES.pop(pid, None)
    return {"ok": True}


@api.get("/__admin/ui", response_class=HTMLResponse)
async def ui():
    return PAGE


PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fake supplier control</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1b1f24;--mute:#667085;--line:#e3e6ea;--acc:#2f6fed;--ok:#1a7f37;--bad:#c0362c}
@media (prefers-color-scheme:dark){:root{--bg:#121417;--card:#1b1e23;--fg:#e8eaed;--mute:#9aa3af;--line:#2c3138;--acc:#6b9bff;--ok:#4cc276;--bad:#ff7a70}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1100px;margin:0 auto;padding:20px}h1{font-size:20px;margin:0 0 4px}h2{font-size:16px;margin:0 0 12px}
.sub{color:var(--mute);margin:0 0 18px}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:16px}
.wrap{overflow-x:auto}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--mute);font-weight:600;font-size:12px}input,select{font:inherit;color:var(--fg);background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:5px 7px}
input[type=number]{width:95px}button{font:inherit;border:1px solid var(--line);background:var(--bg);color:var(--fg);border-radius:6px;padding:5px 10px;cursor:pointer}
button.pri{background:var(--acc);border-color:var(--acc);color:#fff}button.bad{color:var(--bad)}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}
label{display:flex;flex-direction:column;gap:4px;color:var(--mute);font-size:12px}.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
#msg{position:fixed;right:16px;bottom:16px;background:var(--fg);color:var(--bg);padding:8px 12px;border-radius:8px;opacity:0;transition:opacity .2s}
.st-booked{color:var(--ok)}.st-canceled{color:var(--bad)}.st-pending,.st-held{color:var(--acc)}
</style></head><body><main>
<h1>Fake supplier control</h1>
<p class="sub">Changes apply immediately. They reset when the service sleeps or restarts, so use “Re-apply my saved setup” after waking it.</p>

<div class="card"><h2>Global settings</h2><div class="grid">
<label>Tax % added to every price<input id="tax_percent" type="number" step="0.01"></label>
<label>“Will Change Price” increase %<input id="price_change_percent" type="number" step="0.01"></label>
<label>Hotels not listed below<select id="only_listed"><option value="true">Return nothing (only my hotels)</option><option value="false">Return rooms too</option></select></label>
<label>Scenario for hotels not listed below<select id="default_scenario"></select></label>
<label>Timeout scenarios hang (seconds)<input id="timeout_seconds" type="number"></label>
<label>“Confirm Slowly” wait (seconds)<input id="slow_seconds" type="number"></label>
<label>Cancelling the non-refundable booking<select id="nrf_cancel"><option value="refuse">Refused</option><option value="allow">Allowed</option></select></label>
</div><p class="row" style="margin:14px 0 0"><button class="pri" onclick="saveSettings()">Save settings</button></p></div>

<div class="card"><h2>Hotels</h2>
<p class="sub">Price = per room per night, in the search currency. Total = price × (1 + tax %) × nights × rooms.</p>
<div class="wrap"><table><thead><tr><th>Property id</th><th>Scenario</th><th>Price / night</th><th>Example: 1 room, 1 night</th><th></th></tr></thead><tbody id="hotels"></tbody></table></div>
<p class="row" style="margin:14px 0 0"><input id="newId" placeholder="Any property id, e.g. 12345"><button onclick="addHotel()">Add hotel</button></p></div>

<div class="card"><h2>Bookings</h2><div class="wrap"><table><thead><tr><th>Itinerary</th><th>Booking ref</th><th>Hotel</th><th>Room status</th><th>Set status</th></tr></thead><tbody id="bookings"></tbody></table></div>
<p class="row" style="margin:14px 0 0"><button onclick="load()">Refresh</button></p></div>

<div class="card"><h2>Recent searches</h2><p class="sub">Each row is one batch your system sent. “Your hotels asked” shows which of your hotels that batch included.</p>
<div class="wrap"><table><thead><tr><th>Time (UTC)</th><th>Check-in</th><th>Currency</th><th>Hotels asked</th><th>Your hotels asked</th><th>Hotels returned</th></tr></thead><tbody id="searches"></tbody></table></div></div>

<div class="card row"><button class="pri" onclick="reapply()">Re-apply my saved setup</button><button class="bad" onclick="resetAll()">Reset everything to defaults</button></div>
<div id="msg"></div></main>
<script>
let SC={}, S={};
const $=id=>document.getElementById(id);
function toast(t){const m=$('msg');m.textContent=t;m.style.opacity=1;setTimeout(()=>m.style.opacity=0,1800)}
async function api(path,opt={}){const r=await fetch(path,{headers:{'Content-Type':'application/json'},...opt});if(!r.ok)throw new Error(await r.text());return r.json()}
function opts(sel){return Object.entries(SC).map(([k,v])=>`<option value="${k}" ${k===sel?'selected':''}>${v}</option>`).join('')}
function remember(){try{localStorage.setItem('fakeSupplierSetup',JSON.stringify({settings:S.settings,properties:S.properties}))}catch(e){}}
async function load(){
  const [a,s,se]=await Promise.all([api('/__admin'),api('/__admin/settings'),api('/__admin/searches')]);
  $('searches').innerHTML=se.length?se.map(x=>`<tr><td>${x.time}</td><td>${x.checkin||''}</td><td>${x.currency||''}</td><td>${x.asked}</td><td>${x.yours.length?x.yours.join(', '):'none'}</td><td>${x.returned}</td></tr>`).join('')
    :'<tr><td colspan="6" style="color:var(--mute)">No searches since the service started</td></tr>';
  SC=a.scenarios; S={settings:s,properties:a.properties};
  for(const k of ['tax_percent','price_change_percent','timeout_seconds','slow_seconds'])$(k).value=s[k];
  $('default_scenario').innerHTML=opts(s.default_scenario);$('nrf_cancel').value=s.nrf_cancel;$('only_listed').value=String(s.only_listed);
  $('hotels').innerHTML=Object.entries(a.properties).map(([id,p])=>`<tr data-id="${id}">
    <td>${id}</td><td><select class="sc">${opts(p.scenario)}</select></td>
    <td><input class="pr" type="number" step="0.01" min="0" value="${p.price}"></td>
    <td>${(p.price*(1+s.tax_percent/100)).toFixed(2)}</td>
    <td class="row"><button class="pri" onclick="saveHotel('${id}')">Save</button><button class="bad" onclick="removeHotel('${id}')">Remove</button></td></tr>`).join('');
  $('bookings').innerHTML=a.itineraries.length?a.itineraries.slice().reverse().map(i=>`<tr><td>${i.itinerary_id}</td><td>${i.affiliate_reference_id}</td><td>${i.property_id}</td>
    <td>${i.rooms.map(r=>`<span class="st-${r.status}">${r.status}</span>`).join(', ')}</td>
    <td class="row">${['booked','pending','canceled'].map(st=>`<button onclick="setBooking('${i.itinerary_id}','${st}')">${st}</button>`).join('')}</td></tr>`).join('')
    :'<tr><td colspan="5" style="color:var(--mute)">No bookings yet</td></tr>';
}
async function saveSettings(){
  const b={default_scenario:$('default_scenario').value,nrf_cancel:$('nrf_cancel').value,only_listed:$('only_listed').value==='true'};
  for(const k of ['tax_percent','price_change_percent','timeout_seconds','slow_seconds'])b[k]=Number($(k).value);
  await api('/__admin/settings',{method:'POST',body:JSON.stringify(b)});await load();remember();toast('Settings saved');
}
async function saveHotel(id){
  const tr=document.querySelector(`tr[data-id="${id}"]`);
  await api('/__admin/properties/'+encodeURIComponent(id),{method:'POST',body:JSON.stringify({scenario:tr.querySelector('.sc').value,price:Number(tr.querySelector('.pr').value)})});
  await load();remember();toast('Hotel '+id+' saved');
}
async function addHotel(){
  const id=$('newId').value.trim();if(!/^\d+$/.test(id))return toast('Property id must be digits');
  await api('/__admin/properties/'+id,{method:'POST',body:JSON.stringify({scenario:'CONFIRM',price:100})});$('newId').value='';await load();remember();toast('Hotel '+id+' added');
}
async function removeHotel(id){await api('/__admin/properties/'+id,{method:'DELETE'});await load();remember();toast('Hotel '+id+' removed')}
async function setBooking(id,st){await api(`/__admin/itineraries/${id}/status`,{method:'POST',body:JSON.stringify({status:st})});await load();toast('Booking set to '+st)}
async function reapply(){
  let saved;try{saved=JSON.parse(localStorage.getItem('fakeSupplierSetup')||'null')}catch(e){}
  if(!saved)return toast('No saved setup in this browser yet');
  await api('/__admin/settings',{method:'POST',body:JSON.stringify(saved.settings)});
  for(const [id,p] of Object.entries(saved.properties))await api('/__admin/properties/'+id,{method:'POST',body:JSON.stringify({scenario:p.scenario,price:p.price})});
  await load();toast('Saved setup re-applied');
}
async function resetAll(){if(!confirm('Reset hotels, settings and bookings to defaults?'))return;
  await api('/__admin/reset',{method:'POST'});
  await api('/__admin/settings',{method:'POST',body:JSON.stringify({tax_percent:15,price_change_percent:10,default_scenario:'CONFIRM',timeout_seconds:60,slow_seconds:90,nrf_cancel:'refuse',only_listed:true})});
  await load();toast('Reset done')}
load().catch(e=>toast('Could not load: '+e.message));
</script></body></html>"""
