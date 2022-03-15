def get_heur(guess_dict):
    fi = open("/ist/users/canu/debias_nlu/data/nli/heuristics_evaluation_set.txt", "r")

    correct_dict = {}
    first = True

    heuristic_list = []
    subcase_list = []
    template_list = []

    for line in fi:
        if first:
            labels = line.strip().split("\t")
            idIndex = labels.index("pairID")
            first = False
            continue
        else:
            parts = line.strip().split("\t")
            this_line_dict = {}
            for index, label in enumerate(labels):
                if label == "pairID":
                    continue
                else:
                    this_line_dict[label] = parts[index]
            correct_dict[parts[idIndex]] = this_line_dict

            if this_line_dict["heuristic"] not in heuristic_list:
                heuristic_list.append(this_line_dict["heuristic"])
            if this_line_dict["subcase"] not in subcase_list:
                subcase_list.append(this_line_dict["subcase"])
            if this_line_dict["template"] not in template_list:
                template_list.append(this_line_dict["template"])

    heuristic_ent_correct_count_dict = {}
    subcase_correct_count_dict = {}
    template_correct_count_dict = {}
    heuristic_ent_incorrect_count_dict = {}
    subcase_incorrect_count_dict = {}
    template_incorrect_count_dict = {}
    heuristic_nonent_correct_count_dict = {}
    heuristic_nonent_incorrect_count_dict = {}



    for heuristic in heuristic_list:
        heuristic_ent_correct_count_dict[heuristic] = 0
        heuristic_ent_incorrect_count_dict[heuristic] = 0
        heuristic_nonent_correct_count_dict[heuristic] = 0 
        heuristic_nonent_incorrect_count_dict[heuristic] = 0

    for subcase in subcase_list:
        subcase_correct_count_dict[subcase] = 0
        subcase_incorrect_count_dict[subcase] = 0

    for template in template_list:
        template_correct_count_dict[template] = 0
        template_incorrect_count_dict[template] = 0

    raw_result_doc = {} # for mcnemar's test
    labels = [] # for raw gt
    for key in correct_dict:
        traits = correct_dict[key]
        heur = traits["heuristic"]
        subcase = traits["subcase"]
        template = traits["template"]

        guess = guess_dict[key]
        correct = traits["gold_label"]
        labels.append(correct)

        if guess == correct:
            raw_result_doc[key]='yes'
            if correct == "entailment":
                heuristic_ent_correct_count_dict[heur] += 1
            else:
                heuristic_nonent_correct_count_dict[heur] += 1

            subcase_correct_count_dict[subcase] += 1
            template_correct_count_dict[template] += 1
        else:
            raw_result_doc[key]='no'
            if correct == "entailment":
                heuristic_ent_incorrect_count_dict[heur] += 1
            else:
                heuristic_nonent_incorrect_count_dict[heur] += 1
            subcase_incorrect_count_dict[subcase] += 1
            template_incorrect_count_dict[template] += 1

    print("Heuristic entailed results:")
    perc = []
    for heuristic in heuristic_list:
        correct = heuristic_ent_correct_count_dict[heuristic]
        incorrect = heuristic_ent_incorrect_count_dict[heuristic]
        total = correct + incorrect
        percent = correct * 1.0 / total
        perc.append(percent)
        print(heuristic + ": " + str(percent))

    print("")
    print("Heuristic non-entailed results:")
    for heuristic in heuristic_list:
        correct = heuristic_nonent_correct_count_dict[heuristic]
        incorrect = heuristic_nonent_incorrect_count_dict[heuristic]
        total = correct + incorrect
        percent = correct * 1.0 / total
        perc.append(percent)
        print(heuristic + ": " + str(percent))
    avg=sum(perc)/len(perc)    
    print("avg: "+str(avg))
    return labels,avg

def format_label(label):
    if label == "entailment":
        return "entailment"
    else:
        return "non-entailment"
    
def get_ans(ans):
    if ans == 0:
        return 'entailment'
    else:
        return 'non-entailment'