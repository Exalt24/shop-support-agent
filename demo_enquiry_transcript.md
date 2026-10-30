# Enquiry routing demo transcript

**This is a run against FAKES.** A scripted model, a stub shop and an in-process fake Zoho CRM (`agent/zoho_fake.py`, region `au`). No live Zoho account, no Shopify store, no model API and no network. The clock is fixed to Friday 2026-10-30. The lines under "Would be sent to Zoho" are the exact requests the client built; they went to the fake.

## 1. A sales enquiry, and the visitor says yes

Conversation `web-1`, visitor email `priya.nair@example.com`.

- **Visitor:** Hi, we run three cafes and want to talk about wholesale pricing. I'm Priya Nair, priya.nair@example.com
- **Agent (replied):** Thanks, Priya. May I pass your name, your contact details and a short note of what you asked to our sales team so they can follow up? Please reply yes or no.
- **Routed:** intent=sales_enquiry queue=sales follow-up=sales task due in 1 business day | consent=no captured=no lead=none vendor=None
- **Would be sent to Zoho:** nothing

- **Visitor:** Yes please, go ahead.
- **Agent (replied):** Thanks, Priya. I've noted your request for our sales team, and they will follow up with you using the details you gave.
- **Routed:** intent=sales_enquiry queue=sales follow-up=sales task due in 1 business day | consent=yes captured=yes lead=created vendor=SUCCESS
- **Would be sent to Zoho:** 
  - `POST accounts.zoho.com.au/oauth/v2/token  (token refresh, credentials not shown)`
  - `POST www.zohoapis.com.au/crm/v8/Leads/upsert {"data": [{"Last_Name": "Nair", "First_Name": "Priya", "Email": "priya.nair@example.com"}], "duplicate_check_fields": ["Email"]}`
  - `POST www.zohoapis.com.au/crm/v8/Tasks {"data": [{"Subject": "Follow up: new sales enquiry from web chat", "Status": "Not Started", "Priority": "Normal", "Due_Date": "2026-11-02", "What_Id": {"id": "1001"}, "$se_module": "Leads"}]}`
  - `POST www.zohoapis.com.au/crm/v8/Leads/1001/Notes {"data": [{"Parent_Id": {"module": {"api_name": "Leads"}, "id": "1001"}, "Note_Content": "Web chat enquiry (sales enquiry, sales queue). The visitor wrote: \"Hi, we run three cafes and want to talk about wholesale pricing. I'm Priya Nair, [email]\""}]}`

## 2. A quote request, and the visitor says no

Conversation `web-2`, visitor email `miguel.santos@example.com`.

- **Visitor:** Can I get a quote for fitting out 20 desks? Miguel Santos, call me on 0917 123 4567
- **Agent (replied):** Thanks, Miguel. May I pass your name, your contact details and a short note of what you asked to our sales team so they can follow up? Please reply yes or no.
- **Routed:** intent=quote_request queue=sales follow-up=sales task due in 1 business day | consent=no captured=no lead=none vendor=None
- **Would be sent to Zoho:** nothing

- **Visitor:** Not right now, thanks.
- **Agent (replied):** No problem, I won't pass your details to our sales team.
- **Routed:** intent=quote_request queue=sales follow-up=sales task due in 1 business day | consent=no captured=no lead=none vendor=None
- **Would be sent to Zoho:** nothing

## 3. A service request, with consent

Conversation `web-3`, visitor email `sam.lee@example.com`.

- **Visitor:** The compressor at our Cebu site is down. Can someone come out? Sam Lee, sam.lee@example.com, we are Lee Cold Storage
- **Agent (replied):** Thanks, Sam. May I pass your name, your contact details and a short note of what you asked to our service team so they can follow up? Please reply yes or no.
- **Routed:** intent=service_request queue=service follow-up=service task due in 2 business days | consent=no captured=no lead=none vendor=None
- **Would be sent to Zoho:** nothing

- **Visitor:** Yes, that's fine.
- **Agent (replied):** Thanks, Sam. I've noted your request for our service team, and they will follow up with you using the details you gave.
- **Routed:** intent=service_request queue=service follow-up=service task due in 2 business days | consent=yes captured=yes lead=created vendor=SUCCESS
- **Would be sent to Zoho:** 
  - `POST www.zohoapis.com.au/crm/v8/Leads/upsert {"data": [{"Last_Name": "Lee", "First_Name": "Sam", "Email": "sam.lee@example.com", "Company": "Lee Cold Storage"}], "duplicate_check_fields": ["Email"]}`
  - `POST www.zohoapis.com.au/crm/v8/Tasks {"data": [{"Subject": "Book a service visit: request from web chat", "Status": "Not Started", "Priority": "Normal", "Due_Date": "2026-11-03", "What_Id": {"id": "1004"}, "$se_module": "Leads"}]}`
  - `POST www.zohoapis.com.au/crm/v8/Leads/1004/Notes {"data": [{"Parent_Id": {"module": {"api_name": "Leads"}, "id": "1004"}, "Note_Content": "Web chat enquiry (service request, service queue). The visitor wrote: \"The compressor at our Cebu site is down. Can someone come out? Sam Lee, [email], we are Lee Cold Storage\""}]}`

## 4. An angry message with a legal threat

Conversation `web-4`, visitor email `dana.cruz@example.com`.

- **Visitor:** This is the third time nobody has answered my quote. I'm calling my lawyer today.
- **Agent (escalated, legal):** Thanks for telling us. I've passed this to a member of our team, who will reply to you directly. You don't need to do anything else for now.
- **Routed:** (escalated before routing: the floor ran first and the model was never called)
- **Would be sent to Zoho:** nothing

## 5. The same email as scenario 1, a second enquiry the same day

Conversation `web-5`, visitor email `priya.nair@example.com`.

- **Visitor:** Hi again, Priya Nair here. Can you also send details on delivery to Dumaguete?
- **Agent (replied):** Thanks, Priya. May I pass your name, your contact details and a short note of what you asked to our sales team so they can follow up? Please reply yes or no.
- **Routed:** intent=sales_enquiry queue=sales follow-up=sales task due in 1 business day | consent=no captured=no lead=none vendor=None
- **Would be sent to Zoho:** nothing

- **Visitor:** Yes please.
- **Agent (replied):** Thanks, Priya. I've noted your request for our sales team, and they will follow up with you using the details you gave.
- **Routed:** intent=sales_enquiry queue=sales follow-up=sales task due in 1 business day | consent=yes captured=yes lead=noted vendor=SUCCESS
- **Would be sent to Zoho:** 
  - `POST www.zohoapis.com.au/crm/v8/Leads/1001/Notes {"data": [{"Parent_Id": {"module": {"api_name": "Leads"}, "id": "1001"}, "Note_Content": "Web chat enquiry (sales enquiry, sales queue). The visitor wrote: \"Hi again, Priya Nair here. Can you also send details on delivery to Dumaguete?\""}]}`

## What the audit log kept

One line per turn; this is the `enquiry` part of each line that has one. Counts and kinds only: no name, no email, no phone, none of the visitor's words (the customer field is the repo's masked email).

```
p***@example.com  {"intent": "sales_enquiry", "queue": "sales", "consent": false, "captured": false, "lead": "none", "vendor": null, "fields_sent": 0}
p***@example.com  {"intent": "sales_enquiry", "queue": "sales", "consent": true, "captured": true, "lead": "created", "vendor": "SUCCESS", "fields_sent": 3}
m***@example.com  {"intent": "quote_request", "queue": "sales", "consent": false, "captured": false, "lead": "none", "vendor": null, "fields_sent": 0}
m***@example.com  {"intent": "quote_request", "queue": "sales", "consent": false, "captured": false, "lead": "none", "vendor": null, "fields_sent": 0}
s***@example.com  {"intent": "service_request", "queue": "service", "consent": false, "captured": false, "lead": "none", "vendor": null, "fields_sent": 0}
s***@example.com  {"intent": "service_request", "queue": "service", "consent": true, "captured": true, "lead": "created", "vendor": "SUCCESS", "fields_sent": 4}
d***@example.com  (escalated before routing) legal
p***@example.com  {"intent": "sales_enquiry", "queue": "sales", "consent": false, "captured": false, "lead": "none", "vendor": null, "fields_sent": 0}
p***@example.com  {"intent": "sales_enquiry", "queue": "sales", "consent": true, "captured": true, "lead": "noted", "vendor": "SUCCESS", "fields_sent": 0}
```

## End state of the fake CRM

2 leads, 2 tasks, 3 notes after five enquiries (scenario 5 added a note to scenario 1's lead, not a second lead).

