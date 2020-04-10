#
# Copyright (C) 2020 by frePPLe bvba
#
# All information contained herein is, and remains the property of frePPLe.
# You are allowed to use and modify the source code, as long as the software is used
# within your company.
# You are not allowed to distribute the software, either in the form of source code
# or in the form of compiled binaries.
#

from datetime import datetime
import os
from subprocess import call

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction, DEFAULT_DB_ALIAS, connections
from django.db.models import Min
from django.utils.translation import gettext_lazy as _

from ...models import ScheduledTask, Task
from freppledb import VERSION
from freppledb.common.middleware import _thread_locals
from freppledb.common.models import User
from .runworker import launchWorker, runTask


class Command(BaseCommand):
    help = """
    Mode 1: schedule name is passed as argument

    Mode 2: no schedule name is passed
    Creates new tasks in the task list, based on the schedule.
    This command is normally executed automatically, scheduled with the at-command.
    Only in rare situations would you need to run this command manually.
    """
    requires_system_checks = False

    def get_version(self):
        return VERSION

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument(
            "--database",
            default=DEFAULT_DB_ALIAS,
            help="Specify the database to run in.",
        )
        parser.add_argument("--schedule", help="Name of the scheduled task to execute")
        parser.add_argument("--user", dest="user", help="User running the command")
        parser.add_argument(
            "--task",
            type=int,
            help="Task identifier (generated automatically if not provided)",
        )

    def handle(self, *args, **options):
        # Dispatcher
        if options["schedule"]:
            self.executeScheduledTask(*args, **options)
        else:
            self.createScheduledTasks(*args, **options)

    def executeScheduledTask(self, *args, **options):
        database = options["database"]
        if database not in settings.DATABASES:
            raise CommandError("No database settings known for '%s'" % database)
        try:
            schedule = ScheduledTask.objects.using(database).get(
                name=options["schedule"]
            )
        except ScheduledTask.DoesNotExist:
            raise CommandError(
                "No scheduled task found with name '%s' " % options["schedule"]
            )
        if "user" in options and options["user"]:
            try:
                user = User.objects.all().using(database).get(username=options["user"])
            except Exception:
                raise CommandError("User '%s' not found" % options["user"])
        else:
            user = None

        task = None
        now = datetime.now()
        try:
            setattr(_thread_locals, "database", database)
            # Initialize the task
            if "task" in options and options["task"]:
                try:
                    task = Task.objects.all().using(database).get(pk=options["task"])
                except Exception:
                    raise CommandError("Task identifier not found")
                if (
                    task.started
                    or task.finished
                    or task.status != "Waiting"
                    or task.name not in ("runplan", "frepple_run")
                ):
                    raise CommandError("Invalid task identifier")
                task.status = "0%"
                task.started = now
                task.processid = os.getpid()
            else:
                task = Task(
                    name="scheduletasks",
                    submitted=now,
                    started=now,
                    status="0%",
                    user=user,
                    processid=os.getpid(),
                )
            task.save(using=database)

            # The loop that actually executes the tasks
            tasklist = schedule.data.get("tasks", [])
            stepcount = len(tasklist)
            idx = 1
            failed = []
            for step in tasklist:
                steptask = Task(
                    name=step.get("name"),
                    submitted=datetime.now(),
                    arguments=step.get("arguments", ""),
                    user=user,
                    status="Waiting",
                )
                steptask.save(using=database)
                Task.objects.all().using(database).filter(pk=task.id).update(
                    message="Running task %s as step %s of %s"
                    % (steptask.id, idx, stepcount),
                    status="%d%%" % int(idx * 100.0 / stepcount),
                )
                runTask(steptask, database)

                # Check the status
                steptask = Task.objects.all().using(database).get(pk=steptask.id)
                if steptask.status == "Failed":
                    failed.append(steptask.id)
                    if step.get("abort_on_failure", False):
                        task.message = "Failed at step %s of %s" % (idx, len(tasklist))
                        task.status = "Failed"
                        task.finished = datetime.now()
                        task.save(using=database)
                        raise Exception(task.message)
                idx += 1

            # Reread the task from the database and update it
            task = Task.objects.all().using(database).get(pk=task.id)
            task.processid = None
            if failed:
                task.status = "Failed"
                task.message = "Failed at tasks: %s" % ", ".join(failed)
            else:
                task.status = "Done"
                task.message = ""
            task.finished = datetime.now()
            task.save(using=database)

            # TODO Email on success

        except Exception as e:
            if task:

                # TODO Email on failure

                task = Task.objects.all().using(database).get(pk=task.id)
                task.status = "Failed"
                task.message = "%s" % e
                task.finished = datetime.now()
                task.processid = None
                task.save(using=database)
            raise e

        finally:
            setattr(_thread_locals, "database", None)

    def createScheduledTasks(self, *args, **options):
        """
        Task scheduler that looks at the defined schedule and generates tasks
        at the right moment.
        """
        database = options["database"]
        if database not in settings.DATABASES:
            raise CommandError("No database settings known for '%s'" % database)

        # Collect tasks
        # Note: use transaction and select_for_update to handle concurrent access
        now = datetime.now()
        created = False
        with transaction.atomic(using=database):
            for schedule in (
                ScheduledTask.objects.all()
                .using(database)
                .filter(next_run__isnull=False, next_run__lte=now)
                .order_by("next_run", "name")
                .select_for_update(skip_locked=True)
            ):
                Task(
                    name="schedule",
                    submitted=now,
                    status="Waiting",
                    user=schedule.user,
                    arguments=schedule.name,
                ).save(using=database)
                schedule.computeNextRun(now)
                schedule.save(using=database)
                created = True

        # Launch the worker process
        if created:
            launchWorker(database)

        # Reschedule to run this task again at the next date
        earliest_next = (
            ScheduledTask.objects.using(database)
            .filter(next_run__isnull=False)
            .aggregate(Min("next_run"))
        )["next_run__min"]
        print(earliest_next, settings.FREPPLE_CONFIGDIR)
        if earliest_next:
            if os.name == "nt":
                import locale

                locale.setlocale()
                call(
                    [
                        "schtasks",
                        "/create",
                        "/tn",
                        "frepple scheduler",
                        "/sc",
                        "once",
                        "/st",
                        earliest_next.strftime("%H:%M"),
                        "/sd",
                        earliest_next.strftime(
                            "%y-%m-%d"
                        ),  # TODO needs to be locale specific :-(
                        "/tr",
                        "frepplectl scheduletasks --database=%s" % database,
                    ]
                )
            else:
                my_env = os.environ.copy()
                my_env["FREPPLE_CONFIGDIR"] = settings.FREPPLE_CONFIGDIR
                try:
                    retcode = call(
                        [
                            "at",
                            earliest_next.strftime("%H:%M %y-%m-%d"),
                            "frepplectl",
                            "scheduletasks",
                            "--database=%s" % database,
                        ],
                        env=my_env,
                    )
                    if retcode < 0:
                        raise CommandError(
                            "Non-zero exit code when scheduling the task"
                        )
                except OSError as e:
                    raise CommandError("Can't schedule the task: %s" % e)

    # accordion template
    title = _("Group and schedule tasks")
    index = 1000

    help_url = "user-guide/command-reference.html#scheduletasks"

    @staticmethod
    def getHTML(request):
        return "allo allo"