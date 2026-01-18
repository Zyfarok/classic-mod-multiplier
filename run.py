import os
import json
import random
import argparse
import sys
from typing import Iterable
from dotenv import load_dotenv
from ossapi import BeatmapsetSearchCategory, Ossapi, BeatmapsetSearchSort, Score
from time import sleep
from circleguard import Circleguard, Judgment, JudgmentType, ReplayCache, ReplayUnavailableException
from slider import Beatmap, Library, Slider, Spinner
from copy import deepcopy
from ossapi.models import NonLegacyMod

# from circleguard import set_options
# set_options(loglevel="TRACE")
load_dotenv()

if not os.path.exists("library"):
  os.makedirs("library")

api = Ossapi(os.getenv("CLIENT_ID"), os.getenv("CLIENT_SECRET"))
cg = Circleguard(os.getenv("API_KEY"), os.getenv("CLIENT_ID"), os.getenv("CLIENT_SECRET"), "circleguard.db")
library = Library("library")

def get_ranked_maps() -> list[int]:
  ranked_beatmap_ids = []
  cursor = None
  while True:
    res = api.search_beatmapsets(mode=0, explicit_content="show", sort=BeatmapsetSearchSort.RANKED_ASCENDING, cursor=cursor, category=BeatmapsetSearchCategory.RANKED)
    for beatmapset in res.beatmapsets:
      ranked_beatmap_ids += [beatmap.id for beatmap in beatmapset.beatmaps]

    if res.cursor is None:
      break
    cursor = res.cursor
    sleep(1)
  return ranked_beatmap_ids

def judgment_counts(judgments: Iterable[Judgment]):
  counts = {
    "count_300": 0,
    "count_100": 0,
    "count_50": 0,
    "count_miss": 0
  }

  for judgment in judgments:
    if judgment.type == JudgmentType.Hit300:
      counts["count_300"] += 1
    if judgment.type == JudgmentType.Hit100:
      counts["count_100"] += 1
    if judgment.type == JudgmentType.Hit50:
      counts["count_50"] += 1
    if judgment.type == JudgmentType.Miss:
      counts["count_miss"] += 1
  return counts

def object_count(beatmap: Beatmap):
  counts = {
    "sliderends": 0,
    "sliderticks": 0,
    "spinners": 0
  }
  for hit_obj in beatmap.hit_objects(circles=False, sliders=True, spinners=True):
    if isinstance(hit_obj, Slider):
      counts["sliderends"] += 1
      counts["sliderticks"] += hit_obj.ticks - 2
    if isinstance(hit_obj, Spinner):
      counts["spinners"] += 1
  return counts

def get_ranked_beatmap_ids(path):
  if os.path.isfile(path):
    with open(path, "r") as f:
      ranked_beatmap_ids = json.load(f)
  else:
    ranked_beatmap_ids = get_ranked_maps()
    with open(path, "w") as f:
      f.write(json.dumps(ranked_beatmap_ids))
  
  return ranked_beatmap_ids

def _run(replay, score, beatmap, beatmap_id, mods="??", save=False, logging=False, deeplogging=False, output="data.txt"):
  object_c = object_count(deepcopy(beatmap))

  # print(score)
  lazer_score = score.total_score_without_mods
  lazer_accuracy = score.accuracy
  if deeplogging:
    print(f"score.statistics: {score.statistics}")
  lazer_bonus_score = 0
  lazer_bonus_score += 0 if score.statistics.large_bonus is None else score.statistics.large_bonus * 50
  lazer_bonus_score += 0 if score.statistics.small_bonus is None else score.statistics.small_bonus * 10
  lazer_acc_score = 500_000 * pow(lazer_accuracy, 5)
  lazer_combo_score = lazer_score - lazer_bonus_score - lazer_acc_score
  combo_score_no_acc = lazer_combo_score / lazer_accuracy
  if deeplogging:
    print(f"lazer score: {lazer_score}")
    print(f"lazer score decomposition: {lazer_score}")
    print(f" - accuracy: {lazer_accuracy * 100}%")
    print(f" - bonus score: {lazer_bonus_score}")
    print(f" - accuracy score: {lazer_acc_score}")
    print(f" - combo score: {lazer_combo_score}")
    print(f" - combo score without acc: {combo_score_no_acc}")

  lazer_judgments, _ = cg.judgments(replay, beatmap=deepcopy(beatmap), slider_acc=True)
  lazer_calculated = judgment_counts(lazer_judgments)
  stable_judgments, _ = cg.judgments(replay, beatmap=deepcopy(beatmap), slider_acc=False)
  stable_calculated = judgment_counts(stable_judgments)

  if deeplogging:
    print(f"lazer actual judgements: {score.statistics}")
    print(f"circleguard judgements with slider_acc: {lazer_calculated}")
    print(f"circleguard judgements without slider_acc: {stable_calculated}")

  # circleguard judgments doesn't include spinners, so we add them back
  judgement_count_before = sum(stable_calculated.values())
  lazer_diff = {}
  lazer_diff["count_300"] = replay.count_300 - lazer_calculated["count_300"]
  lazer_diff["count_100"] = replay.count_100 - lazer_calculated["count_100"]
  lazer_diff["count_50"] = replay.count_50 - lazer_calculated["count_50"]
  lazer_diff["count_miss"] = replay.count_miss - lazer_calculated["count_miss"]
  stable_calculated["count_300"] += lazer_diff["count_300"]
  stable_calculated["count_100"] += lazer_diff["count_100"]
  stable_calculated["count_50"] += lazer_diff["count_50"]
  stable_calculated["count_miss"] += lazer_diff["count_miss"]
  judgement_count = sum(stable_calculated.values())
  assert object_c["spinners"] == judgement_count - judgement_count_before, "spinner count mismatch"
  
  # circleguard judgments does not include 100s from sliderends (for stable), so we estimate them using lazer statistics:
  sliderends = (0 if score.maximum_statistics.slider_tail_hit is None else score.maximum_statistics.slider_tail_hit)
  assert object_c["sliderends"] == sliderends, "sliderend count mismatch"
  sliderend_missed = sliderends - (0 if score.statistics.slider_tail_hit is None else score.statistics.slider_tail_hit)
  if sliderend_missed > stable_calculated["count_300"]:
    sliderend_missed = stable_calculated["count_300"]
  stable_calculated["count_300"] -= sliderend_missed
  stable_calculated["count_100"] += sliderend_missed

  # Minor note: circleguard judgments also counts sliderbreaks (due to missing slider head) as misses, but we can't detect those here.

  if deeplogging:
    print(f"judgement_diff: {lazer_diff}")
    print(f"sliderend_missed: {sliderend_missed} / {sliderends}")
    print(f"stable estimated judgements: {stable_calculated}")
  
  stable_accuracy = (
    stable_calculated["count_300"] * 300
    + stable_calculated["count_100"] * 100
    + stable_calculated["count_50"] * 50
  ) / (judgement_count * 300)

  stable_combo_score = combo_score_no_acc * stable_accuracy # Ideally we should compute the combo progress "accuracy" differently
  stable_acc_score = 500_000 * pow(stable_accuracy, 5)
  stable_score_estimate = stable_combo_score + stable_acc_score + lazer_bonus_score

  if deeplogging:
    print(f"stable accuracy estimate: {stable_accuracy * 100}%")
    print(f"stable score estimate decomposition: {stable_score_estimate}")
    print(f" - accuracy score: {stable_acc_score}")
    print(f" - combo score: {stable_combo_score}")
    print(f" - bonus score: {lazer_bonus_score} (same as lazer)")

  ratio = lazer_score / stable_score_estimate

  if logging:
    print(f"lazer score:\t\t{lazer_score}")
    print(f"stable score:\t\t{stable_score_estimate}")
    print(f"ideal CL multiplier:\t{ratio}")

  if save:
    with open(output, "a") as f:
        f.write(f"https://osu.ppy.sh/beatmaps/{beatmap_id}\t{replay.user_id}\t{stable_score_estimate}\t{lazer_score}\t{mods}\t{stable_accuracy}\t{lazer_accuracy}\n")

allowed_mods = { "NM", "EZ", "HD", "HR", "DT", "HT", "NC", "FL", "SO", "SD", "PF" }

def run(beatmap_ids=None, amount=1, start = 1, end=50, sample_size=10, path="beatmap_ids/all.json", save=False, logging=False, deeplogging=False, output="data.txt"):
  if beatmap_ids is None:
    ranked_beatmap_ids = get_ranked_beatmap_ids(path)
    beatmap_ids = random.sample(ranked_beatmap_ids, min(amount, len(ranked_beatmap_ids)))

  for i, beatmap_id in enumerate(beatmap_ids):
    if logging:
      print(f"")
      print(f"##################################")
      print(f"processing beatmap {beatmap_id}... ({i} / {len(beatmap_ids)})")
    beatmap_scores = [
      (score.user_id, score)
      for score in api.beatmap_scores(beatmap_id, mode="osu", limit=100).scores
      if (score.legacy_score_id is None) and all(mod.acronym in allowed_mods for mod in score.mods)
    ][max(start - 1, 0):end]
    beatmap_scores = random.sample(beatmap_scores, min(sample_size, len(beatmap_scores)))

    beatmap = None
    for (user_id, score) in beatmap_scores:
      mods = "".join([mod.acronym for mod in score.mods])
      if mods == "":
        mods = "NM"
      if logging:
        print(f"----------------------------")
        print(f"processing user {user_id} ({mods})...")
      try:
        try:
          replay = ReplayCache("circleguard.db", beatmap_id, user_id)
          cg.load(replay)
          replay = replay.replay
        except:
          replay = cg.ReplayMap(beatmap_id, user_id)
        if beatmap is None:
          beatmap = replay.beatmap(library)

        _run(replay, score, beatmap, beatmap_id, mods=mods, save=save, logging=logging, deeplogging=deeplogging, output=output)

        sleep(5)
      except ReplayUnavailableException:
        print("missing replay.", "beatmap:", beatmap_id, "user_id:", user_id)
      except KeyboardInterrupt:
        sys.exit()
        pass
      except:
        print("unable to process replay.", "beatmap:", beatmap_id, "user_id:", user_id)
        sleep(12)

def run_user(beatmap_id, user_id, save=False, logging=False, deeplogging=False, output="data.txt"):
  replay = cg.ReplayMap(beatmap_id, user_id)
  beatmap = replay.beatmap(library)

  score = api.beatmap_user_score(beatmap_id, user_id, mode="osu", mods=replay.mods).score

  print(f"processing user {user_id} on beatmap {beatmap_id} with mods: {score.mods}...")
  _run(replay, score, beatmap, beatmap_id, save=save, logging=logging, deeplogging=deeplogging, output=output)

def main():
  parser = argparse.ArgumentParser()

  parser.add_argument('-b', "--beatmap_ids", type=int, nargs='+')
  parser.add_argument('-c', "--count", type=int, default=1)
  parser.add_argument('-u', "--user_id", type=int)
  parser.add_argument('-s', "--save", action=argparse.BooleanOptionalAction, default=False, help="Save data to file")
  parser.add_argument('-l', "--logging", action=argparse.BooleanOptionalAction, default=False)
  parser.add_argument('-d', "--deeplogging", action=argparse.BooleanOptionalAction, default=False)
  parser.add_argument('--start', type=int, default=1, help="The leaderboard spot to start from")
  parser.add_argument('--end', type=int, default=100, help="The leaderboard spot to end on")
  parser.add_argument('--sample-size', type=int, default=10, help="Amount of replays to use for each beatmap")
  parser.add_argument('--path', type=str, default="beatmap_ids/all.json", help="Path to ranked beatmap ids file")
  parser.add_argument('--output', type=str, default="data.txt", help="Path to output data file")

  args = parser.parse_args()

  if args.user_id is not None and args.beatmap_ids is None:
    print("missing beatmap_id")
    return
  
  if args.user_id is not None:
    run_user(args.beatmap_ids[0], args.user_id, save=args.save, logging=args.logging, deeplogging=args.deeplogging)
    return
  
  run(beatmap_ids=args.beatmap_ids, amount=args.count, start=args.start, end=args.end, sample_size=args.sample_size, save=args.save, logging=args.logging, deeplogging=args.deeplogging, path=args.path, output=args.output)

if __name__ == "__main__":
  main()