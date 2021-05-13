#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Sun Feb 28 20:07:01 2021

@author: clarewest
"""

import pyspark
from pyspark.sql import SparkSession
from pyspark.sql.functions import col, lit, explode, concat_ws, array_contains

## Spark
sc = pyspark.SparkContext()
spark = SparkSession.builder \
           .getOrCreate()

## Data paths
data_path = "data/"
ot_platform = data_path+"OT_platform/21.02/"
ot_genetics = data_path+"OT_genetics/ftp.ebi.ac.uk/pub/databases/opentargets/genetics/20022712/"
targetage = data_path+"targetage/"


## Read Open Targets data 
diseases = (spark.read.parquet(ot_platform+"ETL_parquet/diseases/", header=True)
            .withColumnRenamed("id","diseaseId")
            .withColumnRenamed("name","diseaseName")
            )
targets = (spark.read.parquet(ot_platform+"ETL_parquet/targets/")
           .withColumnRenamed("id","targetId")
           .withColumnRenamed("approvedSymbol", "targetSymbol")
           .withColumnRenamed("approvedName","targetName")
           )
evidences = spark.read.parquet(ot_platform+"ETL_parquet/evidences/succeeded")
knowndrugs = spark.read.parquet(ot_platform+"ETL_parquet/knownDrugs")

# NB otg_evidence has already filtered out evidence with a score < 0.05
otg_evidences = spark.read.parquet(ot_platform+"ETL_parquet/evidences/succeeded/sourceId\=ot_genetics_portal/")

## Genetic data
coloc = spark.read.json(ot_genetics+"v2d_coloc/")
v2d = spark.read.json(ot_genetics+"v2d/")
cs = spark.read.json(ot_genetics+"v2d_credset")
#variants = spark.read.json(ot_genetics+"lut/variant-index/")
studies = spark.read.json(ot_genetics+"lut/study-index/")
overlap = spark.read.json(ot_genetics+"lut/overlap-index/")

## Age-related diseases (ARDs)
ards = spark.read.csv(data_path+"disease_list.csv")
ards = ards.toDF(*["diseaseId", "morbidity"]).filter(~col("diseaseId").contains("#"))
ardiseases = (ards.join(diseases, "diseaseId", "left")
              .select("morbidity","diseaseId","children", "description", "diseaseName", "therapeuticAreas", "descendants")
              )

# Therapeutic areas to exclude
excluded_tas = ["OTAR_0000018", "OTAR_0000014"]

# Get EFO codes etc for descendant diseases
descendant_ardiseases = (
    ardiseases
    .select("diseaseId", "diseaseName", explode("descendants").alias("specificDiseaseId"))
    .join(diseases.withColumnRenamed("diseaseId", "specificDiseaseId").withColumnRenamed("diseaseName", "specificDiseaseName"), ["specificDiseaseId"])   
    .join(ards, "diseaseId")
    .select("morbidity", "diseaseId", "diseaseName", "specificDiseaseId", "specificDiseaseName", "therapeuticAreas", "description")
)

parent_ardiseases = (ardiseases
                     .select("morbidity",
                             "diseaseId", 
                             "diseaseName", 
                             col("diseaseId").alias("specificDiseaseId"), 
                             col("diseaseName").alias("specificDiseaseName"),
                             "therapeuticAreas",
                             "description")
                     .filter(array_contains(col("therapeuticAreas"), excluded_tas[1]))
                     .filter(array_contains(col("therapeuticAreas"), excluded_tas[2]))
                     )

all_ardiseases = parent_ardiseases.union(descendant_ardiseases)


## Association data 
overall_associations = spark.read.parquet(ot_platform+"ETL_parquet/associations/indirect/byOverall/")
associations = spark.read.parquet(ot_platform+"ETL_parquet/associations/indirect/byDatatype/")
gen_associations = associations.filter(col("datatypeId")=="genetic_association")


## Targets with genetic associations 
ard_associations = (ards.join(gen_associations,"diseaseId", "inner"))
ard_associations.groupBy("morbidity").count().show()
# are direct associations included in indirect associations?

## Get annotations for all targets implicated
ard_targets = (ard_associations
               .select("targetId")
               .distinct()
               .join(targets, ["targetId"])
               .drop("hallMarks", "genomicLocation")
               )
   

## Get GWAS evidence
otg_evidence_cols = ["morbidity",
                     "specificDiseaseId", "specificDiseaseName", 
                     "diseaseId", "diseaseName", 
                     "targetId", 
                     "variantId",
                     "diseaseFromSourceId",
                     "variantFunctionalConsequenceId",
                     "publicationYear",
                     "resourceScore",
                     "diseaseFromSource",
                     "oddsRatio",
                     "confidenceIntervalUpper", "confidenceIntervalLower",
                     "variantRsId",
                     "studyId",
                     "studyCases", "studySampleSize",
                     "score"
                     ]

ard_otg_evidences = (all_ardiseases.drop("therapeuticAreas", "description")
                    .join(otg_evidences
                             .withColumnRenamed("diseaseId", "specificDiseaseId")
                             .withColumnRenamed("diseaseLabel", "specificDiseaseName"), 
                             ["specificDiseaseId", "specificDiseaseName"], "inner")
                    .select(otg_evidence_cols)
                    )

ard_studies = (ard_otg_evidences
                .select("morbidity", "studyId", "diseaseId", "diseaseName", "specificDiseaseId", "specificDiseaseName", "studySampleSize", "studyCases")
                .distinct()
                .join(studies.select(col("study_id").alias("studyId")), "studyId")
                )

#ard_v2d = ard_studies.join(v2d.withColumnRenamed("study_id", "studyId"), "studyId")

ard_v2d = spark.read.parquet(data_path+"targetage/ard_v2d.parquet")

## get variant details for ard_v2d variants
#ard_variants = (ard_v2d
#                .select("lead_chrom", "lead_pos", "lead_ref", "lead_alt")
#                .join(variants.select(col("chr_id").alias("lead_chrom"), 
#                                      col("position").alias("lead_pos"), 
#                                      col("ref_allele").alias("lead_ref"), 
#                                      col("alt_allele").alias("lead_alt"))
#                      )
#                )
 


## Get all lead variants for these studies (we aren't interested in tag variants, at the moment)
## Filter to where lead variant ID == tag variant ID so we can include beta/OR/pval for just the lead
ard_leads = (ard_v2d.withColumn("lead_variantId", concat_ws("_", col("lead_chrom"), col("lead_pos"), col("lead_ref"), col("lead_alt")))
             .filter(col("lead_variantId") == concat_ws("_", col("tag_chrom"), col("tag_pos"), col("tag_ref"), col("tag_alt")))
             .select("studyId", 
                     "lead_variantId",
                              "morbidity", 
                              "diseaseId",
                              "diseaseName",
                              "specificDiseaseId",
                              "specificDiseaseName", 
                              "has_sumstats", 
                              "trait_reported", 
                              "n_cases",
                              "n_initial",
                              "n_replication",
                              "ancestry_initial",
                              "ancestry_replication",
                              "num_assoc_loci",
                              "odds_ratio",
                              "oddsr_ci_lower",
                              "oddsr_ci_upper",
                         #     "overall_r2",
                              "beta", 
                              "beta_ci_lower",
                              "beta_ci_upper",
                              "direction",
                              "lead_alt",
                              "lead_chrom",
                              "lead_pos",
                              "lead_ref",
                         #     "log10_ABF",   
                              "pval",
                         #     "pval_exponent"
                            )
             .distinct()
             )

                         

                
# variant ID = chromosome_position_reference_alternative

## Where sumstats are available, we can use colocalisation data
## COLOCALISATION
coloc_studies = (coloc
                 .filter(col("coloc_h4")>=0.8)
                 .filter(col("left_var_right_study_pval")<=5e-8)
                 .withColumn("left_variantId", concat_ws("_", col("left_chrom"), col("left_pos"), col("left_ref"), col("left_alt")))
                 .withColumn("right_variantId", concat_ws("_", col("right_chrom"), col("right_pos"), col("right_ref"), col("right_alt")))
                 .withColumnRenamed("left_study", "left_studyId")
                 .withColumnRenamed("right_study", "right_studyId")
                 )
coloc_studies = coloc_studies.toDF(*(c.replace("left_", "lead_") for c in coloc_studies.columns))

## Question - does coloc include all tag variants too? Around half seem to not be genome-wide significant

## Otherwise, we can use overlap data
# AB overlap - how many variants in possible variants overlap
# A_distinct and B_distinct - how many are distinct to variant A and B
# How should we decide? OT gen says they overlap if at least one overlaps
# In some cases all will overlap 
# I checked and overlap includes both directions so we only have to join on the left 
## OVERLAP
overlap_left = (overlap.withColumn("lead_variantId", concat_ws("_", col("A_chrom"), col("A_pos"), col("A_ref"), col("A_alt")))
                .withColumn("right_variantId", concat_ws("_", col("B_chrom"), col("B_pos"), col("B_ref"), col("B_alt")))
                .withColumnRenamed("AB_overlap", "LR_overlap")
                .withColumnRenamed("A_study_id", "lead_studyId")
                .withColumnRenamed("B_study_id", "right_studyId")
                )

overlap_left = overlap_left.toDF(*(c.replace('A_', 'lead_') for c in overlap_left.columns))
overlap_left = overlap_left.toDF(*(c.replace('B_', 'right_') for c in overlap_left.columns))


#### Need to calculate overlap myself
# if alt/ref are reversed should we count them?

## First find out which combinations have an overlap to minimise contribution
overlapping = overlap.select(col("A_study_id"), col("B_study_id")).distinct()

## Variants from studies with summary stats (UKBB) will have `posterior_prob` that variant is causal
## Those without summary stats will just have `overall_r2`, the LD between tag and lead
## Where there are no summary statistics we want to use the LD cutoff
## Where there are summary statistics we want posterior_prob > 0 (use finemapping instead)
## Using a cutoff of 0.8, the mean number of tag variants per hit decreases from 71.2 (std=135.9) to 32.7 (std=108.6)
## Total number of variants decreases from 1,034,480 to 464,182 (44.9%)

gw_signif_pval = 5e-8
LD_cutoff = 0.7
ard_hits = (ard_v2d
            .withColumn("lead_variantId", concat_ws("_", col("lead_chrom"), col("lead_pos"), col("lead_ref"), col("lead_alt")))
            .withColumn("tag_variantId", concat_ws("_", col("tag_chrom"), col("tag_pos"), col("tag_ref"), col("tag_alt")))
            .select("studyId", "lead_variantId", "tag_variantId", "overall_r2", "posterior_prob", "has_sumstats")
            .distinct()
            .filter(((col("has_sumstats") == True) & (col("posterior_prob") > 0)) | ((col("has_sumstats") == False) & (col("overall_r2") > LD_cutoff)))
     #       .filter(col("lead_variantId") == col("tag_variantId"))
     #       .filter(col("overall_r2")>=LD_cutoff)    ## If we don't want to use finemapping
            .filter(col("pval") <= gw_signif_pval)    ## only drops 51 variants (0.01%)
            )

ard_hits_count = ard_hits.groupBy("studyId", "has_sumstats", "lead_variantId").count()

def prefix_columns(old_df, prefix, id_col = None):    
    columns = old_df.columns
    new_columns = [prefix + c if c != id_col else c for c in columns] 
    column_mapping = [[o, n] for o, n in zip(columns, new_columns)]
    new_df = old_df.select(list(map(lambda old, new: col(old).alias(new),*zip(*column_mapping))))
    return(new_df)

ard_hits_overlap_full = (
    prefix_columns(ard_hits, "A_", "tag_variantId")
    .join(prefix_columns(ard_hits, "B_", "tag_variantId"), "tag_variantId", "inner")
    )

ard_hits_overlap = (
    ard_hits_overlap_full
    .groupBy(["A_studyId", "A_lead_variantId", "B_studyId", "B_lead_variantId"])
    .count()
    .withColumnRenamed("count", "AB_overlap")
    .join(prefix_columns(ard_hits_count, "A_"), ["A_studyId", "A_lead_variantId"])
    .join(prefix_columns(ard_hits_count, "B_"), ["B_studyId", "B_lead_variantId"])
    .withColumn("A_distinct", col("A_count") - col("AB_overlap"))
    .withColumn("B_distinct", col("B_count") - col("AB_overlap")) 
)

## Join with ARD lead variants
cols_to_join = ["studyId", "lead_variantId", "lead_chrom", "lead_pos", "lead_ref", "lead_alt"]

def get_edges(all_method_edges, ard_lead_variants, all_studies):
    ard_edges = (ard_lead_variants
                   .join(all_method_edges.withColumnRenamed("lead_studyId", "studyId"), cols_to_join)   # Get edges just for ARD lead variants 
                   .join(all_studies.select(col("study_id").alias("right_studyId"), col("trait_reported").alias("right_trait_reported"), col("n_cases").alias("right_n_cases"), col("n_initial").alias("right_n_initial"), col("has_sumstats").alias("right_has_sumstats")), "right_studyId", "left")  # Add the trait reported
                   .join(ard_lead_variants.select(col("studyId").alias("right_studyId"), col("morbidity").alias("right_morbidity")).distinct(), "right_studyId", "left")       # see if it's in our ARD list
                   )
    return ard_edges

def is_right_lead(method_edges, all_v2d):
    is_right = (method_edges 
                       .join(all_v2d.
                         withColumn("right_variantId", concat_ws("_", col("lead_chrom"), col("lead_pos"), col("lead_ref"), col("lead_alt")))
                         .select(col("study_id").alias("right_studyId"), "right_variantId")
                         .distinct()
                         .withColumn("right_is_lead", lit(True)), 
                         ["right_studyId", "right_variantId"], 
                         "left")
                   )
    return is_right


coloc_ard_leads = get_edges(coloc_studies, ard_leads, studies)
coloc_ard_leads = is_right_lead(coloc_ard_leads, v2d)


overlap_ard_leads = get_edges(overlap_left, ard_leads, studies)
overlap_ard_leads = is_right_lead(overlap_ard_leads, v2d)


## Save output
if (0):
    # All diseases
    all_ardiseases.toPandas().to_csv(data_path+"full_disease_list.csv", index=False)
    
    # V2D
#    ard_v2d.write.parquet(data_path+"targetage/ard_v2d.parquet")
    
    # ARD associations
    ard_associations.write.parquet(data_path+"targetage/ard_associations.parquet")
    
    # ARD annotations
    ard_targets.write.parquet(data_path+"targetage/ard_annotations.parquet")

    # colocalisation details
    coloc_ard_leads.repartition(1).write.parquet(targetage+"coloc_ard_leads.parquet")
    overlap_ard_leads.repartition(1).write.parquet(targetage+"overlap_ard_leads.parquet")
    ard_leads.repartition(1).write.parquet(targetage+"ard_leads.parquet")






                     

