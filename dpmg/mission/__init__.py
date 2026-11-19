"""Mission planner: catalog, plan file, runner, triggers, map and the GUI tab.

The planner lays out what each radio does and when, checks the plan, and runs
it through the same service commands the SDR panels send. It mounts as the
app's mission view, the /mission page beside the operator and engineering
views (build_mission_tab in mission_tab.py), on the app's own LinkManager, so
it sees the same radios as the SDR tab. Any GUI started from the repo serves
it at /mission on its own web port (8080 for the app, 8081 for a second GUI
started with run_gui_link.py 8081 47841).

User manual: docs/MISSION_PLANNER.md.
"""
