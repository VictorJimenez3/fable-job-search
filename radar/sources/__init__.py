"""Source registry. Each fetcher returns list[Job] and may raise; the pipeline
wraps every call so one dead source never kills a run."""
