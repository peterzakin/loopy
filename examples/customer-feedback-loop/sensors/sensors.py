from loopy import sensor
from loopy.events import CustomerTicket  # generated from registry.yml — for your typechecker


# webhook: Zendesk POSTs a new ticket; you shape it into a CustomerTicket
@sensor(webhook="/hooks/zendesk", emits="CustomerTicket")
def zendesk_tickets(req) -> CustomerTicket:
    ticket = req.json["ticket"]
    return CustomerTicket(
        ticket_id=ticket["id"],
        subject=ticket["subject"],
        body=ticket["description"],
        link=ticket["url"],
    )
