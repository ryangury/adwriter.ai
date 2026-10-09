"""Which optional emails the daily run and the verifier send. Turning one back
on is a one-line change here.

Not listed here, and always sent: the per-ad emails (data summary and tool
feedback), Build Summary, Action Required, the daily CTR email, and the
breakage alerts (ACV Max scraping broken, ReconVision unreachable, benchmark
hung / ran out of time).
"""

# "Ads Ready": the day's finished ads in one email, with each ad's HendrickCars.com
# link (the link lookup only runs when this is on). The per-ad emails carry the ads.
EMAIL_ADS_READY = False

# "Ad Posting Alert": ads not posted / outdated after a verifier pass. The passes
# still update the verification fields and the Inventory page without it.
EMAIL_POSTING_ALERT = False
