from unittest.mock import Mock

from galaxy.jobs import (
    JobConfigurationLimits,
    MinimalJobWrapper,
)
from galaxy.model import (
    GalaxySession,
    Job,
)
from galaxy.model.unittest_utils import GalaxyDataTestApp
from galaxy.model.unittest_utils.data_app import GalaxyDataTestConfig


class MockJobConfig:
    def __init__(self) -> None:
        self.limits = JobConfigurationLimits()

    def get_destinations(self, tag):
        return [create_mock_destination()]


class GalaxyJobConfigApp(GalaxyDataTestApp):
    def __init__(self, config: GalaxyDataTestConfig | None = None, **kwd):
        super().__init__(config, **kwd)
        self.job_config = MockJobConfig()


def create_mock_app():
    return GalaxyJobConfigApp()


def create_job_wrapper(app: GalaxyJobConfigApp, user_id=None, session_id=None):
    if session_id:
        session = GalaxySession(id=session_id)
        app.model.session.add(session)
        app.model.session.commit()
    job = create_mock_job(app, user_id, session_id)
    job_wrapper = MinimalJobWrapper(job, app)  # type: ignore[arg-type]
    return job_wrapper


def create_mock_job(app: GalaxyJobConfigApp, user_id=None, session_id=None, state="new"):
    """Creates a mock job object."""
    job = Job()
    job.user_id = user_id
    job.session_id = session_id
    job.state = state
    app.model.session.add(job)
    app.model.session.commit()
    return job


def create_mock_destination():
    """Creates a mock job destination."""
    job_destination_mock = Mock()
    job_destination_mock.id = "one"
    job_destination_mock.params = {}
    job_destination_mock.runner = "test_runner"
    job_destination_mock.tags = ["one", "two"]
    return job_destination_mock


def test_registered_user_limit():
    app = create_mock_app()
    job_wrapper = create_job_wrapper(app, user_id=1)
    job = job_wrapper.get_job()
    job_destination_mock = create_mock_destination()

    for _ in range(2):
        create_mock_job(app, user_id=1, state="running")

    # Test below limit
    job_wrapper.app.job_config.limits.registered_user_concurrent_jobs = 3
    result = job_wrapper.queue_with_limit(job, job_destination_mock)
    assert result is True

    # Test at limit
    result = job_wrapper.queue_with_limit(job, job_destination_mock)
    assert result is False


def test_anonymous_user_limit():
    app = create_mock_app()
    job_wrapper = create_job_wrapper(app, session_id=1)
    job = job_wrapper.get_job()
    job_destination_mock = create_mock_destination()

    create_mock_job(app, session_id=1, state="running")

    # Test no jobs
    app.job_config.limits.anonymous_user_concurrent_jobs = 0
    result = job_wrapper.queue_with_limit(job, job_destination_mock)
    assert result is False

    # Test below limit
    app.job_config.limits.anonymous_user_concurrent_jobs = 2
    result = job_wrapper.queue_with_limit(job, job_destination_mock)
    assert result is True

    # Test at limit
    result = job_wrapper.queue_with_limit(job, job_destination_mock)
    assert result is False


def test_destination_total_limit():
    app = create_mock_app()
    job_wrapper = create_job_wrapper(app, user_id=1)
    job = job_wrapper.get_job()
    job_destination_mock = create_mock_destination()

    # Test below limit
    app.job_config.limits.destination_total_concurrent_jobs["one"] = 1
    result = job_wrapper.queue_with_limit(job, job_destination_mock)
    assert result is True

    # Test at limit
    result = job_wrapper.queue_with_limit(job, job_destination_mock)
    assert result is False


def test_destination_tag_limit():
    app = create_mock_app()
    job_wrapper = create_job_wrapper(app, user_id=1)
    job = job_wrapper.get_job()
    job_destination_mock = create_mock_destination()

    # Test below limit
    app.job_config.limits.destination_total_concurrent_jobs["two"] = 1
    result = job_wrapper.queue_with_limit(job, job_destination_mock)
    assert result is True

    # Test at limit
    result = job_wrapper.queue_with_limit(job, job_destination_mock)
    assert result is False


def test_resubmitted_jobs_do_not_block_queueing():
    app = create_mock_app()
    limits = app.job_config.limits
    limits.registered_user_concurrent_jobs = 2
    limits.destination_user_concurrent_jobs["one"] = 2
    limits.destination_total_concurrent_jobs["one"] = 2
    limits.destination_total_concurrent_jobs["two"] = 2
    job_destination_mock = create_mock_destination()
    jobs = [create_mock_job(app, user_id=1, state="resubmitted") for _ in range(3)]
    job_wrapper = MinimalJobWrapper(jobs[0], app)  # type: ignore[arg-type]

    assert job_wrapper.queue_with_limit(jobs[0], job_destination_mock) is True
    assert jobs[0].state == Job.states.QUEUED


def test_resubmitted_anonymous_jobs_do_not_block_queueing():
    app = create_mock_app()
    app.job_config.limits.anonymous_user_concurrent_jobs = 1
    job_wrapper = create_job_wrapper(app, session_id=1)
    create_mock_job(app, session_id=1, state="resubmitted")

    assert job_wrapper.queue_with_limit(job_wrapper.get_job(), create_mock_destination()) is True


def test_resubmitted_jobs_do_not_relax_running_count():
    app = create_mock_app()
    app.job_config.limits.destination_user_concurrent_jobs["one"] = 2
    job_destination_mock = create_mock_destination()
    for state in ("running", "queued"):
        create_mock_job(app, user_id=1, state=state).destination_id = "one"
    job = create_mock_job(app, user_id=1, state="resubmitted")
    job_wrapper = MinimalJobWrapper(job, app)  # type: ignore[arg-type]
    app.model.session.commit()

    assert job_wrapper.queue_with_limit(job, job_destination_mock) is False
