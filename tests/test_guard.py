from agent.guard import figures, verify_grounded

FACTS = "Order #1019, placed 2026-09-20. Total: 250.0 USD. Items: 2x USB Audio Interface. Returns within 30 days."


def test_a_reply_made_only_of_the_facts_passes():
    r = verify_grounded("Your order #1019 for 250 USD is paid and not shipped yet. Returns are open for 30 days.", FACTS)
    assert r.ok, r.violations


def test_an_invented_figure_is_refused():
    r = verify_grounded("Your order #1019 will arrive in 4 days.", FACTS)
    assert not r.ok
    assert any("4" in v for v in r.violations)


def test_a_price_that_differs_from_the_facts_is_refused():
    r = verify_grounded("The total was 275 USD.", FACTS)
    assert not r.ok


def test_trailing_zeros_do_not_make_two_spellings_two_numbers():
    assert figures("250.0") == figures("250.00") == figures("250")
    assert verify_grounded("The total is 250 USD.", FACTS).ok


def test_digits_inside_a_product_code_are_not_quantities():
    # The reply names a product whose code contains digits. The facts name it too, but even a code the
    # facts omit must not trip the figure check, or the guard fires on product names and gets switched off.
    r = verify_grounded("The USB-C2 model ships with a cable.", "Product: Cable kit")
    assert r.ok, r.violations


def test_a_refund_commitment_is_refused_even_if_every_figure_is_grounded():
    r = verify_grounded("I've issued a refund of 250 USD for order #1019.", FACTS)
    assert not r.ok
    assert any("authority" in v for v in r.violations)


def test_a_discount_offer_is_refused():
    assert not verify_grounded("I can give you a discount code for your trouble.", FACTS).ok
    assert not verify_grounded("Take 15% off your next order.", FACTS + " 15").ok


def test_unsupported_outcomes_are_refused_even_with_no_figure_in_them():
    # Both came out of the first live run: neither contains a number, so the figure check alone passes them.
    assert not verify_grounded("The tracking details were emailed to you when the order shipped.", FACTS).ok
    assert not verify_grounded("You can cancel it now and the payment will be returned to the original method.", FACTS).ok
    assert not verify_grounded("Please confirm and we'll proceed.", FACTS).ok


def test_a_number_written_as_a_word_is_still_a_figure():
    shipping = "Orders are packed within 2 business days. Delivery takes 3 to 7 business days."
    assert verify_grounded("Orders are packed within two business days, then three to seven business days.", shipping).ok
    refused = verify_grounded("Delivery takes five business days.", shipping)
    assert not refused.ok and any("5" in v for v in refused.violations)


def test_a_number_word_without_a_unit_is_not_a_quantity():
    assert verify_grounded("One of our teammates will reply, and you only need to wait.", "Order #1019").ok


def test_the_policy_wording_about_refunds_still_passes():
    assert verify_grounded("Refunds go back to the original payment method after the item arrives.",
                           "Refunds go back to the original payment method after the item arrives.").ok


def test_figures_the_customer_supplied_may_be_echoed():
    r = verify_grounded("Yes, the 2 interfaces are on order #1019.", FACTS, allowed_extra=frozenset({"2"}))
    assert r.ok, r.violations
