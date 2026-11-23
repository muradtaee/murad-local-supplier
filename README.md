# murad-local-supplier

Fake Expedia Rapid v3 test supplier for credit-hold QA (dev only).
Property ids 99000001-99000011 each follow one scenario; room names start with it ("Will Pending - Double Room").
Admin: GET /__admin, GET /__admin/log, POST /__admin/properties/{id} {"scenario":...}, POST /__admin/itineraries/{id or ref}/status {"status":"booked|canceled|pending"}, POST /__admin/reset.
