# Prompts data dir

Drop `*.txt` files here to override prompts at runtime without modifying Python sources.

Recognised filenames (= the variable names exported from `app/config/prompts.py`):

- `EQUIPMENT_PAYMENT_RATIO_PROMPT.txt`
- `INSTALL_PAYMENT_RATIO_PROMPT.txt`
- `PAYMENT_RATIO_PROMPT.txt`
- `PAYMENT_SUMMARY_RATIO_PROMPT.txt`
- `INSTALL_PAYMENT_SUMMARY_RATIO_PROMPT.txt`
- `WARRANTY_SUMMARY.txt`
- `RESULT_VERIFICATION_PROMPT.txt`
- `PAYMENT_CLAUSE_VALIDATION_PROMPT.txt`
- `PAYMENT_CLAUSE_CATEGORY_PROMPT.txt`
- `RESULT_VERIFICATION_SINGLE_GROUP_PROMPT.txt`

Files are loaded once at process start by `app/config/prompts_loader.py`.
Missing files fall back to the constants defined in `app/config/prompts.py`.
