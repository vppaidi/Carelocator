import os
from pulp import HiGHS_CMD

def highs_solver():
    # Default path matches Azure Web App deploy path
    path = os.getenv("HIGHS_BIN", "/home/site/wwwroot/bin/highs")
    return HiGHS_CMD(path=path, msg=False)
