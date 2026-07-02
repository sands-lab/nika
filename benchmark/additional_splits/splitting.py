## Usage: `python splitting.py --git_path '/home/user/git/nika'`

## Data sources:
# 1. `NIKA Traces.zip`
# The README git page of Nika https://github.com/sands-lab/nika includes a section where 
# authors "disclose a large public dataset [...] with more than 900 reasoning traces". It
# links to the Zenodo Dataset https://zenodo.org/records/17971675 
# This dataset contains a single file "NIKA Traces.zip".
#
# 2. `benchmark_selected_640.csv` 
# The original git of Nika includes 640 (problem, scenario, topo_size) configurations,
# located at: https://github.com/sands-lab/nika/blob/main/benchmark/benchmark_full.csv
#
# 3. `benchmark_selected_150.csv`
# This is a file that we extracted, containing 150 (problem, scenario, topo_size) configurations,
# located e.g. at: https://rnd-gitlab-eu-c.huawei.com/prc-ai4net/epmemcstt/nika/-/blob/agent-agnostic/benchmark/nika_selected.csv
# In the following script, we also show that those 150 configurations are exactly 
# the one used in "NIKA Traces.zip" (and in the paper). The paper is *not* using the full benchmark.

## (environment in ML5: llama3)

import json
from pathlib import Path
import pandas as pd
import os
import zipfile
import numpy as np
import argparse
import shutil
import csv
import sys
import yaml

def unzip_to_folder(zip_path):
    """
    Extract f.zip into a sibling folder f/.
    If the folder already exists, do nothing (pass).
    The .zip file is left untouched.
    """
    if not zip_path.endswith(".zip"):
        raise ValueError(f"Not a .zip file: {zip_path}")

    # /abs/path/f.zip -> /abs/path/f
    target_dir = zip_path[:-len(".zip")]

    if os.path.exists(target_dir):
        return target_dir  # already exists -> pass

    os.makedirs(target_dir)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(target_dir)

    return target_dir

def _load_json(path: Path):
    """Return parsed JSON, or None if the file is missing / malformed."""
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
        
def build_nika_dataframe(base_path) -> pd.DataFrame:
    base = Path(base_path)

    rows = []
    # A session directory is any directory that contains session_meta.json.
    # rglob makes this robust to where exactly `base` points.
    for meta_path in sorted(base.rglob("session_meta.json")):
        session_dir = meta_path.parent

        meta = _load_json(meta_path) or {}
        gt = _load_json(session_dir / "ground_truth.json") or {}
        sub_path = session_dir / "submission.json"
        sub = _load_json(sub_path) or {}

        # Path-derived info: .../<category>/<problem_name>/<session_id>/
        parts = session_dir.parts
        category = parts[-3] if len(parts) >= 3 else None
        problem_folder = parts[-2] if len(parts) >= 2 else None

        # topo_size must be one of s / m / l; anything else (e.g. p4_* scenarios,
        # missing values) becomes "-"
        raw_topo = meta.get("scenario_topo_size")
        topo_size = raw_topo if raw_topo in ("s", "m", "l") else "-"

        rows.append({
            # --- identity / from folder structure ---
            "session_id": session_dir.name,
            "category": category,
            "problem": problem_folder,

            # --- session_meta.json ---
            "meta_session_id": meta.get("session_id"),
            "scenario": meta.get("scenario_name"),
            "topo_size": topo_size,
            "problem_names": meta.get("problem_names"),
            "meta_root_cause_name": meta.get("root_cause_name"),
            "agent_type": meta.get("agent_type"),
            "backend_model": meta.get("backend_model"),
            "start_time": meta.get("start_time"),
            "end_time": meta.get("end_time"),
            "task_description": meta.get("task_description"),
            "session_dir_orig": meta.get("session_dir"),

            # --- ground_truth.json ---
            "gt_is_anomaly": gt.get("is_anomaly"),
            "gt_faulty_devices": gt.get("faulty_devices"),
            "gt_root_cause_name": gt.get("root_cause_name"),

            # --- submission.json ---
            "sub_is_anomaly": sub.get("is_anomaly"),
            "sub_faulty_devices": sub.get("faulty_devices"),
            "sub_root_cause_name": sub.get("root_cause_name"),

            # --- bookkeeping ---
            "has_submission": sub_path.exists(),
            "session_path": str(session_dir),
        })

    df = pd.DataFrame(rows)
    return df

def add_category(df, df_configs, problem_col='problem', cat_col='category'):
    """Attach category by looking up problem only (category is independent of scenario / topo_size)."""
    problem2cat = (df_configs
                .drop_duplicates(problem_col)
                .set_index(problem_col)[cat_col])
    out = df.copy()
    out[cat_col] = out[problem_col].map(problem2cat)
    return out

def add_prefix(df, col='scenario', out='scenario_prefix'):
    """Add a `prefix` column = first underscore-token of `scenario` + '*'.
    e.g. 'ospf_enterprise_dhcp' -> 'ospf*', 'dc_clos_bgp' -> 'dc*', 'p4_int' -> 'p4*'."""
    df = df.copy()
    df[out] = df[col].str.split('_').str[0] + '*'
    return df

def add_prefix_and_category(df_cur, zip_path = '/home/user/git/nika/other_experiments/selected_splits/NIKA Traces.zip'):
    """
    Function for adding prefix and category from the 3 columns list of experiment

    Example usage:
    outfile = '/home/user/git/nika/benchmark/splits/problem2_train.csv'
    zip_path = '/home/user/git/nika/other_experiments/selected_splits/NIKA Traces.zip'
    df_cur = pd.read_csv(outfile)
    add_prefix_and_category(df_cur, zip_path)

    Input: three columns ['problem', 'scenario', 'topo_size'] in the data frame
    Output: five columns ['problem', 'scenario', 'topo_size', 'category', 'scenario_prefix'] in the data frame
    """
    cols = ['problem', 'scenario', 'topo_size']
    if not np.all([x in df_cur.columns for x in cols]):
        raise ValueError('the columns of the df should contain problem, scenario, and topo_size')
    df_cur[cols] = df_cur[cols].apply(lambda c: c.str.strip())
    unzip_to_folder(str(zip_path))
    df = build_nika_dataframe(Path(zip_path).with_suffix(''))
    df_paper = df[['problem', 'scenario', 'topo_size', 'backend_model', 'category']].value_counts().reset_index()
    df_configs = df_paper[['problem', 'scenario', 'topo_size', 'category']].drop_duplicates().sort_values(['problem', 'scenario', 'topo_size', 'category']).reset_index(drop=True)
    df_cur = add_category(df_cur, df_configs)
    df_cur = add_prefix(df_cur)
    return df_cur

def main_csv_splits(git_path, benchmark_additional_splits_inputs_folder = 'benchmark/additional_splits/inputs'):
    git_path = Path(git_path)
    zip_path = git_path / benchmark_additional_splits_inputs_folder / 'NIKA Traces.zip' # the original downloaded traces
    benchmark_full_csv_path = git_path / benchmark_additional_splits_inputs_folder / 'benchmark_selected_640.csv' # 640 elements (all the possible experiments, provided by the authors)
    nika_selected_csv_path = git_path / benchmark_additional_splits_inputs_folder / 'benchmark_selected_150.csv' # 150 elements selected (that, we will show, are exactly following the original traces)

    print("** Part A. Extract and process `NIKA Traces.zip` and ensure that the configurations are exactly those of `benchmark_selected_150.csv`**")
    # Part 1. 

    print(
    """ 
    Step 1: Unzip the folder located in `zip_path`
    """
    )

    unzip_to_folder(str(zip_path))

    print(
    """
    Step 2: Build a pandas DataFrame from an unzipped NIKA Traces dataset.
    """
    )

    """ 
    Expected layout (from https://zenodo.org/records/17971675):
    
        <base>/
            <category>/                 e.g. end_host_failures, link_failures, ...
                <problem_name>/         e.g. dns_service_down, dhcp_service_down, ...
                    <session_id>/       e.g. 1128152847
                        ground_truth.json
                        session_meta.json
                        submission.json
                        conversation_diagnosis_agent.log   (ignored for now)
                        conversation_submission_agent.log  (ignored for now)
    
    One row per session. Logs are ignored. Column names from ground_truth.json and
    submission.json are namespaced (gt_*, sub_*) because both files share the keys
    is_anomaly / faulty_devices / root_cause_name.
    """

    df = build_nika_dataframe(Path(zip_path).with_suffix(''))
    # df[['category']].value_counts() # just take those for the categories, that are already provided by the authors

    ## Column listing:
    # * network columns
    # (The scenario setup and ground-truth state — which network, which topology, what fault was injected, and the true answer)
    # >> category, problem, scenario, topo_size, problem_names, meta_root_cause_name, task_description, gt_is_anomaly, gt_faulty_devices, gt_root_cause_name
    #
    # * agent answer
    # (Who answered and what they submitted (to be checked against the gt_* fields)
    # >> agent_type, backend_model, sub_is_anomaly, sub_faulty_devices, sub_root_cause_name, has_submission
    #
    # * misc
    # (Bookkeeping — ids, timing, paths)
    # >> session_id, meta_session_id, start_time, end_time, session_dir_orig, session_path

    print(
    """
    Step 3: Check that each ('problem', 'scenario', 'topo_size') is computed for the three models and with 2 or 3 repetitions each
    """
    )

    ## Step 3.1 -- Check the repeated experiments:
    df_paper = df[['problem', 'scenario', 'topo_size', 'backend_model', 'category']].value_counts().reset_index() # count from 2 to 3
    # There are 904 experiments in total.
    # There are 446 with "2" repetitions; and only 4 with "3" repetitions.
    # So roughly 900 experiments, with 3 conditions, each repeated twice (2*3*150).
    # And to be exact, 4 experiments are performed not twice but three times, all for the `dc_clos_bgp` scenario:
    # >> ('link_down','m','gpt-5-mini'); ('link_flap','s','gpt-oss:20b'); ('link_flap','m','gpt-oss:20b'); ('link_fragmentation_disabled','s','gpt-oss:20b')
    df_paper

    ## Step 3.2 -- Additional check that the 3 elements with identical ('problem', 'scenario', 'topo_size', 'backend_model') are actually 3 repetitions of the same sample
    sub = df[(df['problem'] == 'link_down') & (df['scenario'] == 'dc_clos_bgp') & (df['topo_size'] == 'm') & (df['backend_model'] == 'gpt-5-mini')]
    varying = [c for c in sub.columns
            if sub[c].apply(repr).nunique(dropna=False) > 1]
    sub[varying]
    # - sub_faulty_devices is *normal* to change, it's from the agent answer. We got [pc_0_1] for the two firsts and [pc_0_1, leaf_router_0_1] for the last one
    # - task_description change is just because of the ordering in presenting the list of devices (typically python dictionary issue), not important
    # Conclusion: the 3 experimental conditions are identical, only the agent answer might change

    ## Step 3.3 -- Sanity check that each model is covering the same problems
    config_cols = ['problem', 'scenario', 'topo_size']
    # set of config tuples covered by each model
    per_model = {
        m: set(g[config_cols].itertuples(index=False, name=None))
        for m, g in df_paper.groupby('backend_model')
    }
    # 1) how many configs per model
    for m, s in per_model.items():
        print(f"{m}: {len(s)} configs")
    # 2) are all three sets identical?
    base = next(iter(per_model.values()))
    all_same = all(s == base for s in per_model.values())
    print("all three cover the same configs:", all_same)
    # 3) if not, show what differs per model
    if not all_same:
        for m, s in per_model.items():
            print(m, "| missing:", base - s, "| extra:", s - base)

    df_configs = (
        df_paper[['problem', 'scenario', 'topo_size', 'category']]
        .drop_duplicates()
        .sort_values(['problem', 'scenario', 'topo_size', 'category'])
        .reset_index(drop=True)
    )

    df_configs

    print(
    """
    Step 4: Sanity check that the output from `NIKA Traces.zip` is what we have from the `benchmark_selected_150.csv` we built
    """
    )

    cols = ['problem', 'scenario', 'topo_size']
    df_file0 = pd.read_csv(nika_selected_csv_path)
    df_file0[cols] = df_file0[cols].apply(lambda c: c.str.strip())   # kill stray spaces
    set_file = set(df_file0[cols].itertuples(index=False, name=None))
    set_mine = set(df_configs[cols].itertuples(index=False, name=None))
    print("file rows:", len(df_file0), "| unique:", len(set_file))
    print("identical up to ordering:", set_file == set_mine)
    #print("in file only:    ", set_file - set_mine)
    #print("in df_configs only:", set_mine - set_file)

    print(
    """
    Conclusion:
    - 904 experiments provided by the authors of NIKA.
    - There are 4 experiments performed three times (list below); all the other experiments are performed twice.
        ('dc_clos_bgp', 'link_down','m','gpt-5-mini');  ('dc_clos_bgp', 'link_flap','s','gpt-oss:20b'); 
        ('dc_clos_bgp', 'link_flap','m','gpt-oss:20b'); ('dc_clos_bgp', 'link_fragmentation_disabled','s','gpt-oss:20b')
    - There are 450 (problem, scenario, topo_size, model) different configurations (each repeated twice; except 4 configs repeated three times)
    - There are 150 (problem, scenario, topo_size), each repeated twice for the three models; except 4 configs repeated three times (i.e. 150*2*3+4=904)
    - We confirm that those 150 configurations are the `benchmark_selected_150.csv` ones.
    - Those 150 configurations contain different topologies and scenarios (not only small topologies).
    """
    )

    print(
    """
    ** Part B. Splitting for evaluation generalization capabilities.**
    Each configuration has 1. a problem, 2. a scenario, and 3. a topo_size; each giving a possible generalization problem.
    """
    )

    df_file = add_category(df_file0, df_configs)
    assert df_file['category'].notna().all(), "some rows got no category"
    df_file

    print(
    """
    Step 1: The NIKA traces contain 150 configurations, but the authors provide overall 640 configurations in `benchmark_selected_640.csv`
    We confirm that the 150 configurations are a subset of the 640 configurations, then separate into two sets: the 150 configurations, and the remaining 490.
    """
    )

    cols = ['problem', 'scenario', 'topo_size']

    df_full0 = pd.read_csv(benchmark_full_csv_path)
    df_full0[cols] = df_full0[cols].apply(lambda c: c.str.strip())   # same cleaning as df_file
    df_full = add_category(df_full0, df_configs)
    # There is a remaining element without category: 
    # (problem: p4_aggressive_detection_thresholds; scenario: p4_bloom_filter), for which
    # from the paper, we know that: "Misaligned sketch thresholds" -> Network under attack
    # So we replace it manually:
    df_full.loc[df_full['problem'] == 'p4_aggressive_detection_thresholds', 'category'] = 'network_under_attack'
    assert df_full['category'].notna().all(), "some rows got no category"

    set_150  = set(df_file[cols].itertuples(index=False, name=None))
    set_full = set(df_full[cols].itertuples(index=False, name=None))

    # 1) confirm the 150 are a subset of the 640
    missing = set_150 - set_full
    print("full rows:", len(df_full), "| unique:", len(set_full))
    print("all 150 present in benchmark:", len(missing) == 0)
    print("missing:", missing)

    # 2) split the 640 into the 150 (in df_file) and the remaining 490
    key = df_full[cols].apply(tuple, axis=1)
    mask = key.isin(set_150)

    df_evaluation   = df_full[mask].reset_index(drop=True)    # the 150
    df_rest = df_full[~mask].reset_index(drop=True)    # the 640 - 150 = 490

    print("df_evaluation  (expect 150):", len(df_evaluation))
    print("df_rest (expect 490):", len(df_rest))

    print(
    """
    Step 2a: List of problem categories.
    - The list of the problems (root causes) is diverse and grouping them is not straightforward by direct grep
    (e.g. very uneven by taking the first block of the string): "host_crash", "dns_port_blocked", "link_down", "icmp_acl_block"...
    - Instead of building our own grouping by direct grep or claude, we use Table 3 from authors in the paper (also `Network issues` table in README),
    that is already encoded into `category` as a subfolder in `NIKA Traces.zip`.
    - Regarding the number of problems (aka network issues, root causes), there are some mismatchs:
    + From Table 3 in paper and README it is 41,
    + From the README image it is 54
    + From the listing from the full data from NIKA Traces it is 55.
    - At the end, even if the list of problems is different, the categories are matching exactly:

    category             | sum per category from Table 3 (41 problems) | sum per category from NIKA Traces data (55 problems)
    link_failures        |                                         156 | 156
    end_host_failures    |                                         154 | 154
    network_node_errors  |                                         47  |  47
    misconfigurations    |                                         137 | 137
    resource_contention  |                                         77  |  77
    network_under_attack |                                         69  |  69
                TOTAL |                                         640 | 640

    - We obtain 6 problem categories: "link_failures", "end_host_failures", "misconfigurations", "resource_contention", "network_under_attack", "network_node_errors"
    """
    )

    # - Number of problems (aka network issues, root causes): from Table 3 and README it is 41. From the README image it is 54. From full data it is 55.
    print(""">Mapping for problems***""")
    cat_order = ['link_failures', 'end_host_failures', 'network_node_errors', 'misconfigurations', 'resource_contention', 'network_under_attack']
    rank = {c: i for i, c in enumerate(cat_order)}
    counts = df_full[['category', 'problem']].value_counts()
    counts = counts.sort_index(
        level='category',
        sort_remaining=False,
        key=lambda idx: idx.map(rank),
    )
    print(counts)

    print(
    """
    Step 2b: List of scenario prefix.
    - There are 12 scenarios, and we decided to take the first block, e.g. dc* for dc_clos_bgp
        'dc_clos_bgp', 'dc_clos_service', 
        'ospf_enterprise_dhcp', 'ospf_enterprise_static',
        'p4_bloom_filter', 'p4_counter', 'p4_int', 'p4_mpls',
        'rip_small_internet_vpn',
        'sdn_clos', 'sdn_star',
        'simple_bgp'
    - Actually authors also provide a list of scenarios in Table 5 (also `Network Scenarios` table in README),
    but not detailed, and not directly accessible from `NIKA Traces.zip`. We decide to keep the grep method, with the following confirmed matching.
    + The matching for dc*, sdn*, p4* is clear. 
    + https://claude.ai/chat/dce0a11b-fffe-4df8-a071-c149e2518d61 for discussing the other categories:
    + rip_small_internet_vpn → ISP backbone network (meshed) is no longer an inference --> the code confirms it: full-mesh internal routers; "mini Internet"; gateway→external zones (core+access); scalable s/m/l; folder intradomain_routing
    + ospf_enterprise_dhcp / _static → Campus (3-tier) --> the code shows: enterprise topology token; OSPF = campus IGP; same intradomain_routing folder, different author bucket
    + for simple bgp, it's another separate category, to keep separate

    Scenario (authors)                       | Description (authors)                                     | our grep | list of scenarios in |
    Data center network (CLOS)               | Multi-tier leaf–spine fabric with edge servers.           | dc*      | 'dc_clos_bgp', 'dc_clos_service'
    Campus network (3-tier)                  | Enterprise core–distribution–access topology.             | ospf*    | 'ospf_enterprise_dhcp', 'ospf_enterprise_static'
    ISP backbone network (meshed)            | Provider-style backbone with core and access nodes.       | rip*     | 'rip_small_internet_vpn'
    SDN-enabled cloud POP fabric (CLOS/star) | SDN fabric with centralized controller and edge switches. | sdn*     | 'sdn_clos', 'sdn_star'
    P4 programmable testbed         | Compact testbed for data-plane algorithms and pipeline validation. | p4*      | 'p4_bloom_filter', 'p4_counter', 'p4_int', 'p4_mpls'
                                            |                                                           | simple*  | 'simple_bgp'
    - We overall have 6 scenario groups: "dc*", "ospf*", "rip*", "sdn*", "p4*", "simple*"
    """
    )

    df_evaluation = add_prefix(df_evaluation)
    df_rest = add_prefix(df_rest)
    df_full = add_prefix(df_full)

    print(""">Mapping for scenarios***""")

    print(df_full.sort_values('scenario_prefix')[['scenario_prefix', 'scenario']].value_counts(sort=False))

    print(
    """
    Step 2c: List of topology sizes.
    - There are 3 topology sizes 's', 'm', 'l', and the authors use '-' when it is not a scalable experiment
    """
    )

    print(""">Mapping for topo_size""")
    print(df_full[['topo_size']].value_counts())

    print(
    """Step 3: Design choice for splitting.
    - First the benchmark_selected_150 containing the 150 configurations has a relatively good diversity, so can be fixed as a general test set common to all experiments
    - Then, the remaining 490 configurations can be split into traing/validation/test (this test is a separate test set, with focus in checking the current generalization capabilities)
    - We build our split on those 490 configurations only (always keeping the original 150 intact).

    > Building the split:
    - First, we can check what is the diversity among category, scenario_prefix, and topo_size among those remaining 490:

    category                 | scenario_prefix | topo_size
    link_failures        138 | dc*    126      | s 132
    end_host_failures    124 | ospf*  102      | m 132
    misconfigurations     96 | sdn*    99      | l 132
    resource_contention   59 | p4*     72      | -  94
    network_under_attack  42 | rip*    69    
    network_node_errors   31 | simple* 22
                        490          490          490

    - Then, we always take: 
    + train with 1 category (out of 6); 
    + then validation with 2 categories (out of 6) to add some knowledge of variability; 
    + finally test on 3 categories (out of 6).

    - This leads to train smaller than validation in general, which is consistent with the GEPA paper (see in Appendix E from page 23):
    set       | train | validation | test
    HotpotQA  | 150 | 300 | 300
    IFBench   | 150 | 300 | 294
    HoVer     | 150 | 300 | 300
    PUPA      | 111 | 111 | 221

    - [category] We take the following splits (3 "seeds" for each), selected to ensure a sufficient size for each set:
    + Seed1: train link_failures 138     / validation misconfigurations, network_under_attack 96+42  / test end_host_failures, resource_contention, network_node_errors 124+59+31
    + Seed2: train end_host_failures 124 / validation misconfigurations, resource_contention  96+59  / test link_failures, network_under_attack, network_node_errors 138+42+31
    + Seed3: train misconfigurations  96 / validation link_failures, resource_contention      138+59 / test end_host_failures, network_under_attack, network_node_errors 124+96+42+31

    - [scenario_prefix]
    + Seed1: train dc*   126 / validation sdn*, rip* 99+69  / test ospf* p4* simple*    102+72+22
    + Seed2: train ospf* 102 / validation sdn*, p4*  99+72  / test dc*, rip*, simple*   126+69+22
    + Seed3: train snd*   99 / validation dc*, p4*   126+72 / test ospf*, rip*, simple* 102+69+22

    - [topo_size]
    + train s / validation m / test l

    - [no generalization]
    + in that case, we take 3 random partition in 3 sets, with train/validation containing 163 elements and test containing 164 elements

    > Overall: in each scenario, there is the available (train, validation) split. And two test sets: 
        1. the test set for checking specifically the generalization capabilities; 
        2. the benchmark_selected_150 that is common to all experiments, and allows to compare the different methodologies
    The size of training, validation and testing sets is as large as possible, but in the actual experiments, a subset can be selected if needed.
    """
    )

    print(">For benchmark_selected_150 test set, checking of the category, topo_size, and scenario_prefix diversity")
    print(df_evaluation[['category']].value_counts())
    print(df_evaluation[['topo_size']].value_counts())
    print(df_evaluation[['scenario_prefix']].value_counts())
    #df_evaluation[['scenario_prefix', 'topo_size']].value_counts()
    # Conclusion: this set has good diversity, we don't need to modify it.
    # In the case where we train only on some prefix or on some topo size (and thus want to check the generalization),
    # we can take a subset of this same evaluation set. It makes the methodology simpler and robust (instead of re-designing n evaluation sets)

    print(">For the remaining set of 490 elements, checking also the category, topo_size, and scenario_prefix diversity")
    print(df_rest[['category']].value_counts())
    print(df_rest[['topo_size']].value_counts())
    print(df_rest[['scenario_prefix']].value_counts())

    d_splitting_wrt_problem = {'problem1': 
    {'train': ['link_failures'],
    'validation': ['misconfigurations', 'network_under_attack'],
    'test': ['end_host_failures', 'resource_contention', 'network_node_errors']},
    'problem2': 
    {'train': ['end_host_failures'],
    'validation': ['misconfigurations', 'resource_contention'],
    'test': ['link_failures', 'network_under_attack', 'network_node_errors']},
    'problem3': 
    {'train': ['misconfigurations'],
    'validation': ['link_failures', 'resource_contention'],
    'test': ['end_host_failures', 'network_under_attack', 'network_node_errors']},
    }

    d_splitting_wrt_scenario = {'scenario1': 
    {'train': ['dc*'],
    'validation': ['sdn*', 'rip*'],
    'test': ['ospf*', 'p4*', 'simple*']},
    'scenario2': 
    {'train': ['ospf*'],
    'validation': ['sdn*', 'p4*'],
    'test': ['dc*', 'rip*', 'simple*']},
    'scenario3': 
    {'train': ['snd*'],
    'validation': ['dc*', 'p4*'],
    'test': ['ospf*', 'rip*', 'simple*']},
    }

    d_splitting_wrt_topo_size = {'topo_size': 
    {'train': ['s'],
    'validation': ['m'],
    'test': ['l']} # discarding '-'
    }

    print(">For the 490 remaining configurations, the splitting is done as follows")
    print(d_splitting_wrt_problem)
    print(d_splitting_wrt_scenario)
    print(d_splitting_wrt_topo_size)

    def subset_function(df_rest, col, list_elems):
        subset = df_rest[df_rest[col].isin(list_elems)]
        # at the end we don't do further random subsetting, we keep the largest possible set
        # subset = subset.sample(n=min(n, len(subset)), random_state=random_state).sort_values(['problem', 'scenario', 'topo_size'])
        return subset

    information_added_regarding_category = True

    if not information_added_regarding_category: # previous default csv format
        split_path = git_path / 'benchmark' / 'additional_splits' / 'outputs' / 'csv'
        columns_output = ['problem', 'scenario', 'topo_size'] # to remove to keep all the columns with 5 elements including `category` and `scenario_prefix`
        split_path.mkdir(parents=True, exist_ok=True)
    else: # more information, can be retrieved with `add_prefix_and_category` function
        split_path = git_path / 'benchmark' / 'additional_splits' / 'outputs' / 'csv'
        columns_output = ['problem', 'scenario', 'topo_size', 'category', 'scenario_prefix']
        split_path.mkdir(parents=True, exist_ok=True)

    for splitting_case in ['problem', 'scenario', 'topo_size']:

        if splitting_case == 'problem':
            splitting_col = 'category'
            d = d_splitting_wrt_problem
        elif splitting_case == 'scenario':
            splitting_col = 'scenario_prefix'
            d = d_splitting_wrt_scenario
        elif splitting_case == 'topo_size':
            splitting_col = 'topo_size'
            d = d_splitting_wrt_topo_size

        for problem in d.keys():
            for subset in d[problem].keys():
                print(f'{problem}_{subset}')
                outfile = split_path / f'{problem}_{subset}.csv'
                #subset_function(df_rest, splitting_col, d[problem][subset]).to_csv(outfile, index=False)
                subset_function(df_rest, splitting_col, d[problem][subset])[columns_output].to_csv(outfile, index=False)

    seeds = [1,2,3]
    for seed in seeds:
        rng = np.random.default_rng(seed)

        # Shuffle the positional indices, then split into 3 groups
        shuffled = rng.permutation(len(df_rest))
        groups = np.array_split(shuffled, 3)

        # Sort each group's indices to restore original order, then select rows
        df3, df2, df1 = (df_rest.iloc[np.sort(g)] for g in groups) # df3 first so it has more elements 164 vs 163 vs 163
        df1[columns_output].to_csv(split_path / f'wo_generalization{seed}_train.csv', index=False)
        df2[columns_output].to_csv(split_path / f'wo_generalization{seed}_validation.csv', index=False)
        df3[columns_output].to_csv(split_path / f'wo_generalization{seed}_test.csv', index=False)

    # Keep benchmark_selected and full_benchmark as outputs (with the additional information added)
    df_full.to_csv(split_path / 'benchmark_selected_640.csv', index=False)
    df_evaluation.to_csv(split_path / 'benchmark_selected_150.csv', index=False)
    # raw without the additional information
    # shutil.copy(benchmark_full_csv_path, git_path / split_path / 'benchmark_selected_640.csv')
    # shutil.copy(nika_selected_csv_path, git_path / split_path / 'benchmark_selected_150.csv')

    # Clean the unzipped `NIKA Traces` folder in the inputs folder
    shutil.rmtree(Path(zip_path).with_suffix(''), ignore_errors=True)

    # Addition selected_32 for quick tests (start)
    nika_selected_32_csv_path = git_path / benchmark_additional_splits_inputs_folder / 'benchmark_selected_32.csv' # 32 elements for quick tests
    df_file32 = pd.read_csv(nika_selected_32_csv_path)
    df_file32[cols] = df_file32[cols].apply(lambda c: c.str.strip())   # kill stray spaces
    df_file32 = add_category(df_file32, df_configs)
    assert df_file32['category'].notna().all(), "some rows got no category"
    set_32  = set(df_file32[cols].itertuples(index=False, name=None))
    # 1) confirm the 32 are a subset of the 150
    missing = set_32 - set_150
    print("full rows:", len(df_file32), "| unique:", len(set_32))
    print("all 32 present in selected:", len(missing) == 0)
    print("missing:", missing)
    # 2) extract the 32 from df_full directly
    key = df_full[cols].apply(tuple, axis=1)
    mask = key.isin(set_32)
    df_evaluation_32 = df_full[mask].reset_index(drop=True) # the 32
    print("df_evaluation  (expect 32):", len(df_evaluation_32))
    df_evaluation_32.to_csv(split_path / 'benchmark_selected_32.csv', index=False)
    # Addition selected_32 for quick tests (end)

    print("END")

    print("Note: an additional `add_prefix_and_category` function is provided to add the prefix and category from an existing df of configurations")

### csv to yaml converter (wo infer = using only the information contained in the csv, i.e. without inferring the missing elements)
def csv_to_yaml_wo_infer_converter(csv_path, yaml_path):
    """
    Convert the test-matrix CSV (problem,scenario,topo_size) into the cases YAML.

    The CSV does NOT contain the per-case `inject:` details (host_name, intf_name,
    ports, rates, etc.). Those values cannot be derived from the CSV, so every
    inject value is emitted as the literal placeholder <MISSING>.

    What the converter CAN do faithfully:
    * preserve the CSV row order
    * map topo_size "-"  ->  null
    * emit, for each problem, the correct *set* of inject keys (in the order the
        reference YAML uses them), so only the values are <MISSING>.

    Usage:
        python3 csv_to_yaml.py input.csv output.yaml
    """
    MISSING = "<MISSING>"

    # Per-problem inject key schema, in the exact key order used by the reference YAML.
    # Keys whose values are quoted strings in the reference (e.g. '8', '30', '6633')
    # are listed in QUOTED so we reproduce the quoting style.
    INJECT_KEYS = {
        "dns_record_error": ["host_name", "target_website", "target_domain"],
        "host_crash": ["host_name"],
        "host_ip_conflict": ["host_name", "host_name_2"],
        "host_incorrect_dns": ["host_name"],
        "host_incorrect_gateway": ["host_name"],
        "host_incorrect_ip": ["host_name"],
        "host_incorrect_netmask": ["host_name", "netmask_prefix"],
        "host_missing_ip": ["host_name"],
        "dhcp_service_down": ["host_name", "host_name_2"],
        "dns_service_down": ["host_name"],
        "host_vpn_membership_missing": ["host_name", "host_name_2"],
        "link_detach": ["host_name", "intf_name"],
        "link_down": ["host_name", "intf_name"],
        "link_flap": ["host_name", "intf_name", "down_time", "up_time"],
        "link_fragmentation_disabled": ["host_name", "intf_name", "mtu"],
        "arp_acl_block": ["host_name"],
        "bgp_acl_block": ["host_name"],
        "dns_port_blocked": ["host_name"],
        "http_acl_block": ["host_name"],
        "icmp_acl_block": ["host_name"],
        "ospf_acl_block": ["host_name"],
        "bgp_asn_misconfig": ["host_name"],
        "bgp_blackhole_route_leak": ["host_name"],
        "bgp_hijacking": ["host_name"],
        "bgp_missing_route_advertisement": ["host_name"],
        "host_static_blackhole": ["host_name"],
        "dhcp_missing_subnet": ["host_name", "host_name_2"],
        "mac_address_conflict": ["host_name", "host_name_2"],
        "ospf_area_misconfiguration": ["host_name"],
        "ospf_neighbor_missing": ["host_name"],
        "p4_aggressive_detection_thresholds": ["host_name"],
        "flow_rule_loop": ["host_name", "host_name_2"],
        "flow_rule_shadowing": ["host_name"],
        "sdn_controller_crash": ["host_name"],
        "southbound_port_block": ["host_name", "southbound_port"],
        "southbound_port_mismatch": ["host_name", "mismatched_port", "original_port"],
        "p4_header_definition_error": ["host_name"],
        "p4_compilation_error_parser_state": ["host_name"],
        "mpls_label_limit_exceeded": ["host_name"],
        "p4_table_entry_misconfig": ["host_name"],
        "p4_table_entry_missing": ["host_name"],
        "bmv2_switch_down": ["host_name"],
        "frr_service_down": ["host_name"],
        "arp_cache_poisoning": ["host_name"],
        "dhcp_spoofed_dns": ["host_name", "host_name_2"],
        "dhcp_spoofed_gateway": ["host_name", "host_name_2"],
        "dhcp_spoofed_subnet": ["host_name", "host_name_2"],
        "web_dos_attack": ["host_name", "attacker_device"],
        "incast_traffic_network_limitation": ["host_name", "rate", "burst", "limit", "delay_ms"],
        "link_bandwidth_throttling": ["host_name", "intf_name", "rate", "burst", "limit"],
        "link_high_packet_corruption": ["host_name", "intf_name", "corruption_percentage"],
        "dns_lookup_latency": ["host_name", "intf_name", "delay_ms"],
        "load_balancer_overload": ["host_name", "duration"],
        "receiver_resource_contention": ["host_name", "duration"],
        "sender_application_delay": ["host_name"],
        "sender_resource_contention": ["host_name", "duration"],
    }
    def topo_value(raw):
        """Map the CSV topo_size token to its YAML representation."""
        raw = (raw or "").strip()
        if raw == "-" or raw == "":
            return "null"
        return raw

    out = ["cases:"]
    unknown = set()

    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            problem = row["problem"].strip()
            scenario = row["scenario"].strip()
            topo = topo_value(row["topo_size"])

            out.append(f"- scenario: {scenario}")
            out.append(f"  topo_size: {topo}")
            out.append(f"  problem: {problem}")
            out.append("  inject:")

            keys = INJECT_KEYS.get(problem)
            if keys is None:
                unknown.add(problem)
                keys = ["host_name"]  # safe default
            for key in keys:
                out.append(f"    {key}: {MISSING}")

    with open(yaml_path, "w") as f:
        f.write("\n".join(out) + "\n")

    if unknown:
        sys.stderr.write(
            "WARNING: no inject schema for problems (defaulted to host_name): "
            + ", ".join(sorted(unknown)) + "\n"
        )

    #if __name__ == "__main__":
    #    if len(sys.argv) != 3:
    #        sys.stderr.write("usage: python3 csv_to_yaml.py <input.csv> <output.yaml>\n")
    #        sys.exit(1)
    #    convert(sys.argv[1], sys.argv[2])

def dump_scalar(v):
    if v is None:
        return "null"
    s = str(v)
    # quote strings that are all digits (e.g. '8', '30', '6633') to match style
    if s.isdigit():
        return f"'{s}'"
    return s

def emit(cases, out_path):
    lines = ["cases:"]
    for c in cases:
        lines.append(f"- scenario: {c.get('scenario')}")
        lines.append(f"  topo_size: {dump_scalar(c.get('topo_size'))}")
        lines.append(f"  problem: {c.get('problem')}")
        inj = c.get("inject")
        if isinstance(inj, dict):
            lines.append("  inject:")
            for k, v in inj.items():
                lines.append(f"    {k}: {dump_scalar(v)}")
    with open(out_path, "w") as f:
        f.write("\n".join(lines) + "\n")

def extract_matched_yaml_experiments(argv):
    """
    Extract the cases that MATCH between two YAML files (on the
    (scenario, topo_size, problem) triple, using the same resync/alignment as
    compare_cases.py) and write them to a new YAML.

    The emitted cases are taken from yaml1 (full objects, including their inject
    blocks) so the output keeps whatever detail yaml1 carries. Order is preserved.

    Usage:
        python3 extract_matched.py <yaml1 large> <yaml2 smaller> <out.yaml>
    """
    if len(argv) != 3:
        sys.stderr.write("usage: python3 extract_matched.py <yaml1> <yaml2> <out.yaml>\n")
        return 2
    p1, p2, out = argv

    def load_cases(path):
        with open(path) as f:
            data = yaml.safe_load(f)
        return data["cases"] if isinstance(data, dict) else data

    def triple(c):
        return (c.get("scenario"), c.get("topo_size"), c.get("problem"))

    A = load_cases(p1)
    B = load_cases(p2)
    TA = [triple(c) for c in A]
    TB = [triple(c) for c in B]

    i = j = 0
    matched = []
    while i < len(TA) and j < len(TB):
        if TA[i] == TB[j]:
            matched.append(A[i])   # keep yaml1's full object
            i += 1
            j += 1
        else:
            # A[i] missing from B at this point: advance A only, hold B[j]
            i += 1

    emit(matched, out)
    print(f"yaml1 = {p1}: {len(A)} cases")
    print(f"yaml2 = {p2}: {len(B)} cases")
    print(f"matched written to {out}: {len(matched)} cases")
    return 0

    #if __name__ == "__main__":
    #    sys.exit(main(sys.argv[1:]))

def csv_to_yaml_converter(csv_path, out_path, full_path):
    """
    Given a CSV subset (columns: problem,scenario,topo_size) and the COMPLETE
    reference YAML (the fully-filled 640-case file), produce a fully-filled YAML
    containing exactly the CSV's rows -- with the real inject details looked up
    from the complete YAML.

    Key = (scenario, topo_size, problem). This is unique across the 640 cases,
    so every CSV row maps to exactly one full case. CSV row order is preserved.

    CSV topo_size token "-" (or empty) maps to YAML null, matching the reference.

    Usage:
        csv_to_yaml_converter(<subset.csv>, <out.yaml>, <complete.yaml>)

    Exits non-zero if any CSV row has no match (or, optionally, a duplicate key
    is found in the reference).
    """
    # "usage: python3 complete_from_csv.py <subset.csv> <out.yaml> <complete.yaml>\n"

    def norm_topo(raw):
        raw = (raw or "").strip()
        if raw in ("-", "", "null", "None"):
            return None
        return raw

    def key_of(scenario, topo, problem):
        return (str(scenario).strip(), topo, str(problem).strip())

    full = yaml.safe_load(open(full_path))
    full_cases = full["cases"] if isinstance(full, dict) else full

    # Build lookup, and verify uniqueness of the reference keys.
    lookup = {}
    dup_keys = []
    for c in full_cases:
        k = key_of(c.get("scenario"), c.get("topo_size"), c.get("problem"))
        if k in lookup:
            dup_keys.append(k)
        lookup[k] = c
    if dup_keys:
        sys.stderr.write(
            f"ERROR: reference YAML has {len(dup_keys)} duplicate key(s); "
            "the triple is not unique. Aborting.\n")
        for k in dup_keys[:10]:
            sys.stderr.write(f"  dup: {k}\n")
        return 3

    out_cases = []
    missing = []
    with open(csv_path, newline="") as f:
        for n, row in enumerate(csv.DictReader(f)):
            k = key_of(row["scenario"], norm_topo(row["topo_size"]), row["problem"])
            hit = lookup.get(k)
            if hit is None:
                missing.append((n, k))
            else:
                out_cases.append(hit)

    if missing:
        sys.stderr.write(f"ERROR: {len(missing)} CSV row(s) not found in reference:\n")
        for n, k in missing[:20]:
            disp = tuple("null" if x is None else x for x in k)
            sys.stderr.write(f"  csv row {n}: {disp}\n")
        # still write what matched, but signal failure
        emit(out_cases, out_path)
        print(f"wrote {len(out_cases)} matched cases to {out_path} ({len(missing)} unmatched)")
        return 1

    emit(out_cases, out_path)
    print(f"CSV rows           : {len(out_cases)}")
    print(f"all matched        : yes")
    print(f"written to         : {out_path}")
    return 0

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--git_path",
        default='/home/user/git/nika',
        help="path to the nika git repository",
    )
    # git_path = '/home/user/git/nika'

    ## Perform the csv splits ----
    args = parser.parse_args()
    git_path = args.git_path
    main_csv_splits(git_path)

    ## Convert `benchmark_selected_640.csv` into yaml ----
    benchmark_selected_640_csv_filepath = Path(git_path) / 'benchmark' / 'additional_splits' / 'outputs' / 'csv' / 'benchmark_selected_640.csv'
    benchmark_selected_640_yaml_tmp_filepath = Path(git_path) / 'benchmark' / 'additional_splits' / 'outputs' / 'tmp_benchmark_selected_640.yaml'
    csv_to_yaml_wo_infer_converter(benchmark_selected_640_csv_filepath, benchmark_selected_640_yaml_tmp_filepath)
    # there are <MISSING> in the output, because the new yaml shape is more complete, and we didn't infer those elements yet
    # we now fill the <MISSING> elements by looking to the existing `benchmark_full.yaml`
    # 'benchmark_full.yaml' is currently at the following location, but I cannot assume the order will be always the same
    # benchmark_full_filepath = Path(git_path) / 'benchmark' / 'benchmark_full.yaml'
    # So I took a copy of it that will not change (for now, 685 experiments included, with the 640 original ones in the exact same order)
    # (note: uniqueness is confirmed: all 685 cases have a distinct (scenario, topo_size, problem) key, so the matching to the 640 should be correct)
    benchmark_full_filepath = Path(git_path) / 'benchmark' / 'additional_splits' / 'inputs' / 'benchmark_full.yaml'
    benchmark_selected_640_yaml_filepath = Path(git_path) / 'benchmark' / 'additional_splits' / 'outputs' / 'benchmark_selected_640.yaml'
    argv = (benchmark_full_filepath, benchmark_selected_640_yaml_tmp_filepath, benchmark_selected_640_yaml_filepath)
    extract_matched_yaml_experiments(argv)  # shape of arvg: (input large, input small, output small) --> in the process, the missing elements of small are completed
    benchmark_selected_640_yaml_tmp_filepath.unlink(missing_ok=True) # clean the tmp elements
    # *Conclusion*: the output `benchmark_selected_640.yaml` has the 640 fully completed experiments, 
    # matchs the old csv (in size) and the new yaml (subset of the full 685 experiments) 

    ## Convert all the other csv into yaml (using `benchmark_selected_640.yaml` as a pivot to infer the <MISSING> elements) ----
    general_output_path = Path(git_path) / 'benchmark' / 'additional_splits' / 'outputs'
    csv_dir = general_output_path / 'csv'
    # all the csv to be converted (without the pivot!)
    files = sorted(f.name for f in csv_dir.glob('*.csv') if f.name != 'benchmark_selected_640.csv')
    for csv_file_to_convert in files:
        # csv_file_to_convert = 'benchmark_selected_32.csv'
        print(f"{csv_file_to_convert} to convert to yaml")
        current_input_csv = csv_dir / csv_file_to_convert
        current_output_yaml = general_output_path / (Path(csv_file_to_convert).stem + '.yaml')
        benchmark_selected_640_yaml = Path(git_path) / 'benchmark' / 'additional_splits' / 'outputs' / 'benchmark_selected_640.yaml'
        # python3 complete_from_csv.py current_input_csv benchmark_selected_640_yaml current_output_yaml
        csv_to_yaml_converter(current_input_csv, current_output_yaml, benchmark_selected_640_yaml)

    # Also copy the current benchmark_full_685 and benchmark_selected_56 benchmarks ----
    # (I put the number to indicate the number of current experiments, need to be updated if this is changing)
    # benchmark_selected_56 is fully included into benchmark_full_685
    # There are 55 out of 56 cases that are included in the 640 file (missing: (ospf_enterprise_dhcp, s, dhcp_spoofed_subnet))
    shutil.copy(Path(git_path) / 'benchmark' / 'benchmark_full.yaml'    , git_path / general_output_path / 'benchmark_full_685.yaml')
    shutil.copy(Path(git_path) / 'benchmark' / 'benchmark_selected.yaml', git_path / general_output_path / 'benchmark_selected_56.yaml')
