from YourIpoBot.core.pipeline import YourIpoBotPipeline

pipeline = YourIpoBotPipeline()
pipeline.job_day_end_reconciliation()
print("PIPELINE_RUN_COMPLETED")
