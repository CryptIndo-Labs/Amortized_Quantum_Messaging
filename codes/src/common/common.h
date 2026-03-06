#ifndef COMMON_H
#define COMMON_H

#include <string>
#include <vector>
#include "../../include/json.hpp"

using namespace std;

using json=nlohmann::json;

enum Coin
{
	GOLD=0,
	SILVER=1,
	BRONZE=2
};

struct MintedCoin
{
	string user_id;
	int key_id;
	Coin coin;
	string public_key_hex;
	string signature_hex;
	
	json to_json() const
	{
		return
		{
			{"user",user_id},
			{"kid",key_id},
			{"coin",coin},
			{"pk",public_key_hex},
			{"sig",signature_hex}
		};
	}
	
	static MintedCoin from_json(const json& j)
	{
		return
		{
			j.at("user").get<string>(),
			j.at("kid").get<int>(),
			j.at("coin").get<Coin>(),
			j.at("pk").get<string>(),
			j.at("sig").get<string>()
		};
	}
		
};

struct GhostPacket
{
	string recipient_id;
	int key_id_used;
	Coin coin_used;
	string ciphertext_block;
	string payload_block;
	string nonce_hex;
	
	json to_json() const
	{
		return
		{
			{"to", recipient_id},
			{"kid",key_id_used},
			{"coin",coin_used},
			{"ct",ciphertext_block},
			{"payload",payload_block},
			{"iv",nonce_hex}
		};
	}
	
	static GhostPacket from_json(const json& j)
	{
		return 
		{
			j.at("to").get<string>(),
			j.at("key_id_used").get<int>(),
			j.at("coin").get<Coin>(),
			j.at("ct").get<string>(),
			j.at("payload").get<string>(),
			j.at("iv").get<string>()
		};
	}
};
#endif
