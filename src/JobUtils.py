# Copyright (C) 2018-2026 by xcentaurix
# License: GNU General Public License v3.0


from Plugins.SystemPlugins.JobCockpit.JobSupervisor import JobSupervisor


def getPendingJobs(plugin_id="", as_tuples=False):
    return JobSupervisor.getInstance().getPendingJobs(plugin_id, as_tuples)


def getPendingJob(plugin_id="", path=""):
    jobs = getPendingJobs(plugin_id)
    for job in jobs:
        if job.target_path == path:
            return job
    return None
