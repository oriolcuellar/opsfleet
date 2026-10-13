from retail_analyst.pii import clean_rows, mask_text


def test_emails_and_phones_are_masked():
    text, count = mask_text("Contact jane.doe@example.com or (555) 123-4567.")
    assert "jane.doe" not in text and "123-4567" not in text
    assert count == 2


def test_numbers_and_money_are_not_masked():
    text, count = mask_text("Revenue was $1,234,567.89 across 3,456 orders in 2025.")
    assert count == 0
    assert "1,234,567.89" in text


def test_pii_columns_are_dropped_from_rows():
    rows = clean_rows([{"user_id": 1, "email": "a@b.com", "note": "mail a@b.com"}])
    assert rows == [{"user_id": 1, "note": "mail [EMAIL REMOVED]"}]
