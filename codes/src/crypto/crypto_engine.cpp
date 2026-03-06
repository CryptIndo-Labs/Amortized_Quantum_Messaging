#include "crypto_engine.h"
#include <oqs/oqs.h>
#include <sodium.h>
#include <iostream>
#include <iomanip>
#include <sstream>

CryptoEngine::CryptoEngine()
{
 	if(sodium_init()<0)
 	{
 		cerr<<"Sodium Init Failed"<<endl;
 		exit(1);
 	}
 	OQS_init();
}
pair<string, string> CryptoEngine::generate_keypair(Coin coin)
{
	if(coin==GOLD || coin==SILVER)
	{
		OQS_KEM *kem=OQS_KEM_new(OQS_KEM_alg_kyber_768);
		if(!kem)
			return {"",""};
		vector<uint8_t> pk(kem->length_public_key);
		vector<uint8_t> sk(kem->length_secret_key);
		if(OQS_KEM_keypair(kem,pk.data(),sk.data())!=OQS_SUCCESS)
		{
			OQS_KEM_free(kem);
			return {"",""};
		}
		OQS_KEM_free(kem);
		return {to_hex(pk),to_hex(sk)};
	}
	else 
	{
		vector<uint8_t> pk(crypto_box_PUBLICKEYBYTES);
		vector<uint8_t> sk(crypto_box_SECRETKEYBYTES);
		crypto_box_keypair(pk.data(),sk.data());
		return {to_hex(pk),to_hex(sk)};
	}
}
string CryptoEngine::sign_key(const string& pk_hex, Coin coin)
{
	return "SIG_"+pk_hex.substr(0,8);
}

string CryptoEngine::to_hex(const vector<uint8_t>& data)
{
	stringstream ss;
	for(uint8_t byte : data)
		ss<<hex<<setw(2)<<setfill('0')<<(int)byte;
	return ss.str();
}
